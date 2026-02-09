frappe.ui.form.on("File", {
	preview_file: function (frm) {	
		if (!frm.doc.file_url || !frm.doc.file_url.includes("/api/method/")) {
			return;
		}

		let $preview = "";
		let file_name = frm.doc.file_name || "";
		let file_url = frm.doc.file_url;

		if (frappe.utils.is_image_file(file_name)) {
			$preview = $(`<div class="img_preview">
				<img
					class="img-responsive"
					src="${frappe.utils.escape_html(file_url)}"
					onerror="${frm.toggle_display("preview", false)}"
				/>
			</div>`);
		} else if (frappe.utils.is_video_file(file_name)) {
			$preview = $(`<div class="img_preview">
				<video width="480" height="320" controls>
					<source src="${frappe.utils.escape_html(file_url)}">
					${__("Your browser does not support the video element.")}
				</video>
			</div>`);
		} else if (file_name.toLowerCase().endsWith(".pdf")) {
			$preview = $(`<div class="img_preview">
				<object style="background:#323639;" width="100%">
					<embed
						style="background:#323639;"
						width="100%"
						height="1190"
						src="${frappe.utils.escape_html(file_url)}" type="application/pdf"
					>
				</object>
			</div>`);
		} else if (file_name.toLowerCase().endsWith(".mp3")) {
			$preview = $(`<div class="img_preview">
				<audio width="480" height="60" controls>
					<source src="${frappe.utils.escape_html(file_url)}" type="audio/mpeg">
					${__("Your browser does not support the audio element.")}
				</audio>
			</div>`);
		}

		if ($preview) {
			frm.toggle_display("preview", true);
			frm.get_field("preview_html").$wrapper.html($preview);
		}
	},
});
