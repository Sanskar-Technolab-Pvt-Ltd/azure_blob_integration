__version__ = "0.0.1"
import frappe
from frappe.core.doctype.file.file import File
from frappe.handler import optimize_image, upload_file as core_upload_file

import os
import shutil
from pathlib import Path
from urllib.parse import urlparse
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from frappe import _
from frappe.utils import cint
from frappe.core.doctype.file.utils import update_existing_file_docs   # FIXED
from frappe.handler import check_write_permission, ALLOWED_MIMETYPES, is_whitelisted
from mimetypes import guess_type
from datetime import datetime, timedelta

# ? Cached BlobServiceClient (reduce object creation)
def _get_blob_service_client(connection_string):
    """
    Returns cached BlobServiceClient to avoid creating multiple instances
    """
    try:
        cache = getattr(frappe.local, "_azure_blob_service_client_cache", None)
        if cache and cache.get("cs") == connection_string and cache.get("client"):
            return cache["client"]
    except Exception:
        pass

    client = BlobServiceClient.from_connection_string(connection_string)
    try:
        frappe.local._azure_blob_service_client_cache = {"cs": connection_string, "client": client}
    except Exception:
        pass
    return client


# ? Extract AccountName + AccountKey from connection string
def _get_account_name_key(connection_string):
    """
    Parse connection string to extract account name and key
    """
    try:
        conn_dict = dict(item.split('=', 1) for item in connection_string.split(';') if '=' in item)
        account_name = conn_dict.get('AccountName')
        account_key = conn_dict.get('AccountKey')
        if not account_name or not account_key:
            return None, None
        return account_name, account_key
    except Exception:
        return None, None


# ? Generate SAS URL (works even when PublicAccessNotPermitted)
def _get_sas_url(connection_string, container, blob_key, file_name=None, expiry_seconds=120):
    """
    Generate Azure Blob SAS URL with read permission
    """
    account_name, account_key = _get_account_name_key(connection_string)
    if not account_name or not account_key:
        frappe.throw(_("Azure SAS generation failed: connection string must include AccountName and AccountKey."))

    blob_service_client = _get_blob_service_client(connection_string)
    blob_client = blob_service_client.get_blob_client(container=container, blob=blob_key)

    # ? Content-Disposition filename (optional)
    cd = None
    if file_name:
        cd = f'attachment; filename="{file_name}"'

    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_key,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(seconds=int(expiry_seconds or 120)),
        content_disposition=cd,
    )

    return f"{blob_client.url}?{sas_token}"


# ? Public signed redirect (Guest allowed)
@frappe.whitelist(allow_guest=True)
def generate_public_file(key=None, file_name=None):
    """
    Generate signed URL for public files (guest access allowed)
    """
    if not key:
        frappe.local.response['body'] = "Key not found."
        return

    azure_settings = frappe.get_single("Azure File Upload Settings")
    if not azure_settings.enable:
        frappe.throw(_("Azure Storage is disabled."))

    connection_string = azure_settings.connection_string
    if not connection_string:
        frappe.throw(_("Azure connection string is missing."))

    public_container = azure_settings.public_container_name

    signed_url = _get_sas_url(
        connection_string=connection_string,
        container=public_container,
        blob_key=key,
        file_name=file_name,
        expiry_seconds=120,
    )

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = signed_url
    return


# ? Private signed redirect (login required + permission)
@frappe.whitelist()
def generate_file(key=None, file_name=None):
    """
    Generate signed URL for private files (requires login and permission)
    """
    if not key:
        frappe.local.response['body'] = "Key not found."
        return

    if frappe.session.user == "Guest":
        raise frappe.PermissionError

    # ? Permission check using File doc (Frappe-like)
    try:
        f = frappe.get_all(
            "File",
            filters={"content_hash": key, "is_private": 1},
            fields=["name"],
            limit=1
        )
        if f:
            file_doc = frappe.get_doc("File", f[0].name)
            frappe.has_permission("File", doc=file_doc, ptype="read", throw=True)
        else:
            raise frappe.PermissionError
    except Exception:
        raise frappe.PermissionError

    azure_settings = frappe.get_single("Azure File Upload Settings")
    if not azure_settings.enable:
        frappe.throw(_("Azure Storage is disabled."))

    connection_string = azure_settings.connection_string
    if not connection_string:
        frappe.throw(_("Azure connection string is missing."))

    private_container = azure_settings.private_container_name

    signed_url = _get_sas_url(
        connection_string=connection_string,
        container=private_container,
        blob_key=key,
        file_name=file_name,
        expiry_seconds=120,
    )

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = signed_url
    return


# ? Override handle_is_private_changed
def custom_handle_is_private_changed(self):
    from pathlib import Path
    from azure.storage.blob import BlobServiceClient
    from urllib.parse import urlparse

    # ? Allow Azure-managed files even though they are remote
    if self.is_remote_file and "blob.core.windows.net" not in (self.file_url or "") and "/api/method/" not in (self.file_url or ""):
        return

    # ? Fetch Azure Settings
    azure_settings = frappe.get_single("Azure File Upload Settings")
    enable_azure = azure_settings.enable
    connection_string = azure_settings.connection_string
    public_container = azure_settings.public_container_name
    private_container = azure_settings.private_container_name

    old_file_url = self.file_url
    file_name = self.file_url.split("/")[-1] if self.file_url else self.file_name

    # ? CASE 1: FILE IS IN AZURE
    if old_file_url and ("blob.core.windows.net" in old_file_url or "/api/method/" in old_file_url):

        # ? IF NOR ENABLE AZURE, THROW ERROR
        if not enable_azure:
            frappe.throw(_("Azure Storage is disabled. Cannot change privacy of Azure files."))

        if not connection_string:
            frappe.throw(_("Azure connection string is missing."))

        try:
            # ? GET BLOB KEY FROM content_hash
            blob_key = (self.content_hash or "").strip()

            if not blob_key:
                frappe.throw(_("Cannot change privacy: Blob key not found"))

            # ? Initialize Azure Client
            blob_service_client = _get_blob_service_client(connection_string)

            # ? DETERMINE SOURCE AND DESTINATION CONTAINERS
            if cint(self.is_private):
                source_container = public_container
                target_container = private_container
            else:
                source_container = private_container
                target_container = public_container

            # ? COPY BLOB TO TARGET CONTAINER
            source_blob = blob_service_client.get_blob_client(container=source_container, blob=blob_key)
            target_blob = blob_service_client.get_blob_client(container=target_container, blob=blob_key)

            # ? START COPY - Use SAS URL as source (because public access is disabled)
            sas_source_url = _get_sas_url(
                connection_string=connection_string,
                container=source_container,
                blob_key=blob_key,
                file_name=self.file_name,
                expiry_seconds=300,
            )
            target_blob.start_copy_from_url(sas_source_url)

            # ? WAIT UNTIL COPY COMPLETES (POLLING STATUS)
            import time
            props = target_blob.get_blob_properties()
            attempts = 0
            while props.copy.status == "pending":
                attempts += 1
                time.sleep(0.3)
                props = target_blob.get_blob_properties()
                if attempts > 100:
                    frappe.throw(_("Azure copy is taking too long. Please try again."))

            if props.copy.status != "success":
                frappe.throw(_("Azure blob copy failed: {0}").format(props.copy.status))

            # ? DELETE ORIGINAL BLOB
            source_blob.delete_blob()

            # ? UPDATE file_url BASED ON PRIVACY
            if cint(self.is_private):
                # ? FOR PRIVATE FILES, USE API METHOD URL
                method = "prompt_azure_blob_integration.generate_file"
                self.file_url = f"/api/method/{method}?key={blob_key}&file_name={file_name}"
            else:
                # ? FOR PUBLIC FILES ALSO USE API METHOD URL (because public access is disabled)
                method = "prompt_azure_blob_integration.generate_public_file"
                self.file_url = f"/api/method/{method}?key={blob_key}&file_name={file_name}"

            # ? UPDATE REFERENCE IN ATTACHED DOCTYPES
            update_existing_file_docs(self)

            if (
                self.attached_to_doctype
                and self.attached_to_name
                and self.fetch_attached_to_field(old_file_url)
            ):
                if frappe.get_meta(self.attached_to_doctype).issingle:
                    frappe.db.set_single_value(
                        self.attached_to_doctype,
                        self.attached_to_field,
                        self.file_url,
                    )
                else:
                    frappe.db.set_value(
                        self.attached_to_doctype,
                        self.attached_to_name,
                        self.attached_to_field,
                        self.file_url,
                    )

        except Exception as e:
            frappe.throw(f"Failed to move file in Azure Storage: {str(e)}")

    # ? CASE 2: LOCAL FILE HANDELING (ORIGINAL UNTOUCHED LOGIC)
    else:
        if self.is_remote_file:
            return

        from pathlib import Path
        import shutil

        old_file_url = self.file_url
        file_name = self.file_url.split("/")[-1]
        private_file_path = Path(frappe.get_site_path("private", "files", file_name))
        public_file_path = Path(frappe.get_site_path("public", "files", file_name))

        if cint(self.is_private):
            source = public_file_path
            target = private_file_path
            url_starts_with = "/private/files/"
        else:
            source = private_file_path
            target = public_file_path
            url_starts_with = "/files/"
        updated_file_url = f"{url_starts_with}{file_name}"

        # ? AVOID UNNECESSARY MOVES
        if updated_file_url == old_file_url:
            return

        if not source.exists():
            frappe.throw(
                _("Cannot find file {} on disk").format(source),
                exc=FileNotFoundError,
            )
        if target.exists():
            frappe.throw(
                _("A file with same name {} already exists").format(target),
                exc=FileExistsError,
            )

        # ? USES os.rename WHICH IS ATOMIC
        shutil.move(source, target)
        self.flags.original_path = {"old": source, "new": target}
        frappe.db.after_rollback.add(self.on_rollback)

        self.file_url = updated_file_url
        update_existing_file_docs(self)

        if (
            not self.attached_to_doctype
            or not self.attached_to_name
            or not self.fetch_attached_to_field(old_file_url)
        ):
            return

        if frappe.get_meta(self.attached_to_doctype).issingle:
            frappe.db.set_single_value(
                self.attached_to_doctype,
                self.attached_to_field,
                self.file_url,
            )
        else:
            frappe.db.set_value(
                self.attached_to_doctype,
                self.attached_to_name,
                self.attached_to_field,
                self.file_url,
            )


# ? Override on_trash (Logs Removed)
def custom_on_trash(self):
    from azure.storage.blob import BlobServiceClient
    from urllib.parse import urlparse

    # ? ORIGINAL LOGIC UNCHANGED
    if self.is_home_folder or self.is_attachments_folder:
        frappe.throw(_("Cannot delete Home and Attachments folders"))

    self.validate_empty_folder()
    self.validate_protected_file()
    self._delete_file_on_disk()

    if not self.is_folder:
        self.add_comment_in_reference_doc("Attachment Removed", self.file_name)

    # ? AZURE DELETION LOGIC
    # ? CHECK IS THE FILE IS IN AZURE STORAGE
    if self.content_hash and (("blob.core.windows.net" in (self.file_url or "")) or ("/api/method/" in (self.file_url or ""))):
        azure_settings = frappe.get_single("Azure File Upload Settings")

        if not azure_settings.enable:
            frappe.throw(_("Azure Storage is disabled. Cannot delete file stored in Azure."))

        connection_string = azure_settings.connection_string
        if not connection_string:
            frappe.throw(_("Azure connection string is missing. Cannot delete file stored in Azure."))

        try:
            # ? BLOB KEY IS STORED IN content_hash FIELD
            blob_key = (self.content_hash or "").strip()

            if not blob_key:
                frappe.throw(_("Azure blob key not found. Cannot delete file stored in Azure."))

            # ? DETERMINE CONTAINER BASED ON PRIVACY
            container = (
                azure_settings.private_container_name
                if cint(self.is_private)
                else azure_settings.public_container_name
            )

            # ? INITIALIZE AZURE CLIENT
            blob_service_client = _get_blob_service_client(connection_string)
            blob_client = blob_service_client.get_blob_client(container=container, blob=blob_key)

            # ? DELETE THE BLOB FROM AZURE
            blob_client.delete_blob()

        except Exception as e:
            frappe.throw(f"Failed to delete file from Azure Storage: {str(e)}")


# ? Override upload_file
@frappe.whitelist(allow_guest=True)
def custom_upload_file():
    import os
    from mimetypes import guess_type
    from azure.storage.blob import BlobServiceClient

    # ? FETCH AZURE SETTINGS
    azure_settings = frappe.get_single("Azure File Upload Settings")
    enable_azure = azure_settings.enable
    connection_string = azure_settings.connection_string
    public_container = azure_settings.public_container_name
    private_container = azure_settings.private_container_name

    user = None
    if frappe.session.user == "Guest":
        if frappe.get_system_settings("allow_guests_to_upload_files"):
            ignore_permissions = True
        else:
            raise frappe.PermissionError
    else:
        user: "User" = frappe.get_doc("User", frappe.session.user)
        ignore_permissions = False

    files = frappe.request.files
    is_private = frappe.form_dict.is_private
    doctype = frappe.form_dict.doctype
    docname = frappe.form_dict.docname
    fieldname = frappe.form_dict.fieldname
    file_url = frappe.form_dict.file_url
    folder = frappe.form_dict.folder or "Home"
    method = frappe.form_dict.method
    filename = frappe.form_dict.file_name
    optimize = frappe.form_dict.optimize
    content = None

    # ? IF IT'S AN EXISTING FILE REFERENCE
    if library_file := frappe.form_dict.get("library_file_name"):
        frappe.has_permission("File", doc=library_file, throw=True)
        doc = frappe.get_value(
            "File",
            frappe.form_dict.library_file_name,
            ["is_private", "file_url", "file_name"],
            as_dict=True,
        )
        is_private = doc.is_private
        file_url = doc.file_url
        filename = doc.file_name

    # ? PERMISSION CHECK
    if not ignore_permissions:
        check_write_permission(doctype, docname)

    # ? PROCESS FILE UPLOAD
    if "file" in files:
        file = files["file"]
        content = file.stream.read()
        filename = file.filename

        content_type = guess_type(filename)[0]
        if optimize and content_type and content_type.startswith("image/"):
            args = {"content": content, "content_type": content_type}
            if frappe.form_dict.max_width:
                args["max_width"] = int(frappe.form_dict.max_width)
            if frappe.form_dict.max_height:
                args["max_height"] = int(frappe.form_dict.max_height)
            content = optimize_image(**args)

    frappe.local.uploaded_file_url = file_url
    frappe.local.uploaded_file = content
    frappe.local.uploaded_filename = filename

    # ? FILE TYPE VALIDATION FOR GUESTS
    if content is not None and (frappe.session.user == "Guest" or (user and not user.has_desk_access())):
        filetype = guess_type(filename)[0]
        if filetype not in ALLOWED_MIMETYPES:
            frappe.throw(_("You can only upload JPG, PNG, PDF, TXT, CSV or Microsoft documents."))

    # ? CREATE OR UPDATE FILE DOCUMENT
    if method:
        method = frappe.get_attr(method)
        is_whitelisted(method)
        result = method()
    else:
        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "attached_to_doctype": doctype,
                "attached_to_name": docname,
                "attached_to_field": fieldname,
                "folder": folder,
                "file_name": filename,
                "file_url": file_url,
                "is_private": cint(is_private),
                "content": content,
            }
        )
        file_doc.save(ignore_permissions=ignore_permissions)

        # ? FILE DOCUMENT REFERENCE
        result = file_doc

    # ? AZURE UPLOAD LOGIC
    if enable_azure and content and filename:
        try:    

            # ? GET CURRENT YEAR
            current_year = datetime.now().year

            # ? GET CURRENT MONTH NAME
            current_month = datetime.now().strftime("%B")

            # ? SET BLOG AND FILE PATH
            blob=f"{current_year}/{current_month}/{result.file_name}"

            # ? SELECT CONTAINER BASED ON PRIVACY
            container = private_container if cint(is_private) else public_container

            # ? UPLOAD TO AZURE USING FILE DOCUMENT NAME TO AVOID COLLISIONS
            blob_service_client = _get_blob_service_client(connection_string)
            blob_client = blob_service_client.get_blob_client(container=container, blob=blob_key)
            blob_client.upload_blob(content, overwrite=True)

            # ? SET FILE URL BASED ON PRIVACY
            if cint(is_private):
                # ? FOR PRIVATE FILES, USE API METHOD URL (SIGNED URL WILL BE GENERATED ON ACCESS)
                method = "prompt_azure_blob_integration.generate_file"
                azure_url = f"/api/method/{method}?key={blob}&file_name={result.file_name}"
            else:
                # ? FOR PUBLIC FILES ALSO USE API METHOD URL (because public access is disabled)
                method = "prompt_azure_blob_integration.generate_public_file"
                azure_url = f"/api/method/{method}?key={blob}&file_name={result.file_name}"

            # ? DELETE LOCAL FILE (PUBLIC/PRIVATE)
            local_path = frappe.get_site_path("private" if result.is_private else "public", "files", result.file_name)
            if os.path.exists(local_path):
                os.remove(local_path)

            # ? UPDATE FILE DOCTYPE WITH AZURE URL AND STORE BLOB KEY IN content_hash
            result.db_set("file_url", azure_url)
            result.db_set("content_hash", blob)

        except Exception as e:
            # ? IF ANY ERROR: DELETE LOCAL FILE AND FILE DOC
            try:
                local_path = frappe.get_site_path("private" if result.is_private else "public", "files", result.file_name)
                if os.path.exists(local_path):
                    os.remove(local_path)
            except:
                # ? IGNORE IF FILE NOT FOUND
                pass

            # ? DELETE FILE DOC
            frappe.delete_doc("File", result.name, force=True)

            # ? RAISE ERROR TO USER
            frappe.log_error(f"Azure upload error: {str(e)}", "Azure Upload Error")
            frappe.throw(f"File upload failed: There is a server error.", title="Azure Upload Error")

    return result


# ? MONKEY PATCHING
File.handle_is_private_changed = custom_handle_is_private_changed
File.on_trash = custom_on_trash
frappe.handler.upload_file = custom_upload_file
