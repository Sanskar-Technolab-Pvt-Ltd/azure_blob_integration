__version__ = "0.0.1"
import frappe
from frappe.core.doctype.file.file import File
from frappe.handler import User, optimize_image, upload_file as core_upload_file

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


# -------------------------
# 1. Override handle_is_private_changed
# -------------------------
def custom_handle_is_private_changed(self):
    from pathlib import Path
    from azure.storage.blob import BlobServiceClient
    from urllib.parse import urlparse

    # Allow Azure-managed files even though they are remote
    if self.is_remote_file and "blob.core.windows.net" not in (self.file_url or ""):
        return

    # Fetch Azure Settings
    azure_settings = frappe.get_single("Azure File Upload Settings")
    enable_azure = azure_settings.enable
    connection_string = azure_settings.connection_string
    public_container = azure_settings.public_container_name
    private_container = azure_settings.private_container_name

    old_file_url = self.file_url
    file_name = self.file_url.split("/")[-1]

    # --- CASE 1: File is in Azure ---
    if old_file_url and "blob.core.windows.net" in old_file_url:
        if not enable_azure:
            frappe.throw(_("Azure Storage is disabled. Cannot change privacy of Azure files."))

        try:
            # Parse container from old URL
            parsed_url = urlparse(old_file_url)
            path_parts = parsed_url.path.strip("/").split("/")
            current_container = path_parts[0]  # Extract current container from URL

            # Initialize Azure Client
            blob_service_client = BlobServiceClient.from_connection_string(connection_string)

            # Determine source and destination containers
            if cint(self.is_private):
                source_container = public_container
                target_container = private_container
            else:
                source_container = private_container
                target_container = public_container

            # Validate if source matches current container (safety check)
            if current_container != source_container:
                frappe.throw(_("Azure container mismatch! Expected {0} but found {1}").format(source_container, current_container))

            # Copy Blob to target container
            source_blob = blob_service_client.get_blob_client(container=source_container, blob=self.file_name)
            target_blob = blob_service_client.get_blob_client(container=target_container, blob=self.file_name)

            # Start copy
            target_blob.start_copy_from_url(source_blob.url)

            # Wait until copy completes (polling status)
            props = target_blob.get_blob_properties()
            while props.copy.status == "pending":
                props = target_blob.get_blob_properties()

            # Delete original blob
            source_blob.delete_blob()

            # Update file_url to new container URL
            self.file_url = target_blob.url

            # Update references in attached doctypes
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

    # --- CASE 2: Local File Handling (original untouched logic) ---
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

        # Avoid unnecessary move
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

        # Uses os.rename which is atomic
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


# -------------------------
# 2. Override on_trash (Logs Removed)
# -------------------------
def custom_on_trash(self):
    from azure.storage.blob import BlobServiceClient
    from urllib.parse import urlparse

    # --- Original Logic (Unchanged) ---
    if self.is_home_folder or self.is_attachments_folder:
        frappe.throw(_("Cannot delete Home and Attachments folders"))

    self.validate_empty_folder()
    self.validate_protected_file()
    self._delete_file_on_disk()

    if not self.is_folder:
        self.add_comment_in_reference_doc("Attachment Removed", self.file_name)

    # --- Azure Deletion Logic ---
    # Check if file is in Azure Storage
    if self.file_url and "blob.core.windows.net" in self.file_url:
        azure_settings = frappe.get_single("Azure File Upload Settings")

        if not azure_settings.enable:
            frappe.throw(_("Azure Storage is disabled. Cannot delete file stored in Azure."))

        connection_string = azure_settings.connection_string

        try:
            # Parse URL to extract container and blob name
            parsed_url = urlparse(self.file_url)
            path_parts = parsed_url.path.strip("/").split("/")
            container = path_parts[0]  # Container name from URL
            blob_name = self.file_name  # We stored blob using file_name

            # Initialize Azure Client
            blob_service_client = BlobServiceClient.from_connection_string(connection_string)
            blob_client = blob_service_client.get_blob_client(container=container, blob=blob_name)

            # Delete the blob from Azure
            blob_client.delete_blob()

        except Exception as e:
            frappe.throw(f"Failed to delete file from Azure Storage: {str(e)}")


# -------------------------
# 3. Override upload_file
# -------------------------
@frappe.whitelist(allow_guest=True)
def custom_upload_file():
    import os
    from mimetypes import guess_type
    from azure.storage.blob import BlobServiceClient

    # Fetch Azure Settings
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

    # If it's an existing file reference
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

    # Permission check
    if not ignore_permissions:
        check_write_permission(doctype, docname)

    # Process file upload
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

    # File type validation for guests
    if content is not None and (frappe.session.user == "Guest" or (user and not user.has_desk_access())):
        filetype = guess_type(filename)[0]
        if filetype not in ALLOWED_MIMETYPES:
            frappe.throw(_("You can only upload JPG, PNG, PDF, TXT, CSV or Microsoft documents."))

    # Create or update File document
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
        result = file_doc  # File document reference

    # Azure Upload Logic
    if enable_azure and content and filename:
        try:
            # Select container based on privacy
            container = private_container if cint(is_private) else public_container

            # Upload to Azure using File document name to avoid collisions
            blob_service_client = BlobServiceClient.from_connection_string(connection_string)
            blob_client = blob_service_client.get_blob_client(container=container, blob=result.file_name)
            blob_client.upload_blob(content, overwrite=True)

            # Azure file URL
            azure_url = blob_client.url

            # Delete local file (public/private)
            local_path = frappe.get_site_path("private" if result.is_private else "public", "files", result.file_name)
            if os.path.exists(local_path):
                os.remove(local_path)

            # Update File DocType with Azure URL
            result.db_set("file_url", azure_url)

        except Exception as e:
            # If any error: delete local file and File doc
            try:
                local_path = frappe.get_site_path("private" if result.is_private else "public", "files", result.file_name)
                if os.path.exists(local_path):
                    os.remove(local_path)
            except:
                pass  # Ignore if already deleted

            # Delete File doc
            frappe.delete_doc("File", result.name, force=True)

            # Raise error to user
            frappe.throw(f"File upload failed: There is a server error.", title="Azure Upload Error")

    return result


# -------------------------
# Monkey-patching
# -------------------------
File.handle_is_private_changed = custom_handle_is_private_changed
File.on_trash = custom_on_trash
frappe.handler.upload_file = custom_upload_file
