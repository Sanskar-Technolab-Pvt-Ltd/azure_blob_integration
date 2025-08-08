__version__ = "0.0.1"
import frappe
from frappe.core.doctype.file.file import File
from frappe.handler import optimize_image, upload_file as core_upload_file

import os
import shutil
from pathlib import Path
from urllib.parse import urlparse
from azure.storage.blob import BlobServiceClient
from frappe import _
from frappe.utils import cint
from frappe.core.doctype.file.utils import update_existing_file_docs   # FIXED
from frappe.handler import check_write_permission, ALLOWED_MIMETYPES, is_whitelisted
from mimetypes import guess_type
from datetime import datetime

# ? Override handle_is_private_changed
def custom_handle_is_private_changed(self):
    from pathlib import Path
    from azure.storage.blob import BlobServiceClient
    from urllib.parse import urlparse

    # ? Allow Azure-managed files even though they are remote
    if self.is_remote_file and "blob.core.windows.net" not in (self.file_url or ""):
        return

    # ? Fetch Azure Settings
    azure_settings = frappe.get_single("Azure File Upload Settings")
    enable_azure = azure_settings.enable
    connection_string = azure_settings.connection_string
    public_container = azure_settings.public_container_name
    private_container = azure_settings.private_container_name

    old_file_url = self.file_url
    file_name = self.file_url.split("/")[-1]

    # ? CASE 1: FILE IS IN AZURE
    if old_file_url and "blob.core.windows.net" in old_file_url:

        # ? IF NOR ENABLE AZURE, THROW ERROR
        if not enable_azure:
            frappe.throw(_("Azure Storage is disabled. Cannot change privacy of Azure files."))

        try:
            # ? PARSE CONTAINER FROM OLD URL
            parsed_url = urlparse(old_file_url)
            path_parts = parsed_url.path.strip("/").split("/")

            # ? EXTRACT CURRENT CONTAINER FROM URL
            current_container = path_parts[0]

            # ? Initialize Azure Client
            blob_service_client = BlobServiceClient.from_connection_string(connection_string)

            # ? DETERMINE SOURCE AND DESTINATION CONTAINERS
            if cint(self.is_private):
                source_container = public_container
                target_container = private_container
            else:
                source_container = private_container
                target_container = public_container

            # ? VALIDATE IS SOURCE MATCHES CURRENT CONTAINER (SAFETY CHECK)
            if current_container != source_container:
                frappe.throw(_("Azure container mismatch! Expected {0} but found {1}").format(source_container, current_container))

            # ? COPY BLOB TO TARGET CONTAINER
            source_blob = blob_service_client.get_blob_client(container=source_container, blob=self.file_name)
            target_blob = blob_service_client.get_blob_client(container=target_container, blob=self.file_name)

            # ? START COPY
            target_blob.start_copy_from_url(source_blob.url)

            # ? WAIT UNTIL COPY COMPLETES (POLLING STATUS)
            props = target_blob.get_blob_properties()
            while props.copy.status == "pending":
                props = target_blob.get_blob_properties()

            # ? DELETE ORIGINAL BLOB
            source_blob.delete_blob()

            # ? UPDATE file_url TO NEW CONTAINER URL
            self.file_url = target_blob.url

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
    if self.file_url and "blob.core.windows.net" in self.file_url:
        azure_settings = frappe.get_single("Azure File Upload Settings")

        if not azure_settings.enable:
            frappe.throw(_("Azure Storage is disabled. Cannot delete file stored in Azure."))

        connection_string = azure_settings.connection_string

        try:
            # ? PARSE URL TO EXTRACT CONTAINER AND BLOB NAME
            parsed_url = urlparse(self.file_url)
            path_parts = parsed_url.path.strip("/").split("/")

            # ? CONTAINER NAME FROM URL
            container = path_parts[0]

            # ? BLOB NAME IS THE FILE NAME
            blob_name = self.file_name

            # ? INITIALIZE AZURE CLIENT
            blob_service_client = BlobServiceClient.from_connection_string(connection_string)
            blob_client = blob_service_client.get_blob_client(container=container, blob=blob_name)

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
            blob_service_client = BlobServiceClient.from_connection_string(connection_string)
            blob_client = blob_service_client.get_blob_client(container=container, blob=blob)
            blob_client.upload_blob(content, overwrite=True)

            # ? AZURE FILE URL
            azure_url = blob_client.url

            # ? DELETE LOCAL FILE (PUBLIC/PRIVATE)
            local_path = frappe.get_site_path("private" if result.is_private else "public", "files", result.file_name)
            if os.path.exists(local_path):
                os.remove(local_path)

            # ? UPDATE FILE DOCTYPE WITH AZURE URL
            result.db_set("file_url", azure_url)

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
            frappe.throw(f"File upload failed: There is a server error.", title="Azure Upload Error")

    return result


# ? MONKEY PATCHING
File.handle_is_private_changed = custom_handle_is_private_changed
File.on_trash = custom_on_trash
frappe.handler.upload_file = custom_upload_file
