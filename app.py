# app.py
from flask import Flask, request, render_template, redirect, url_for, send_file
import pandas as pd
import os
import qrcode
import base64
from datetime import datetime
import glob
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
import tempfile
from zoneinfo import ZoneInfo

app = Flask(__name__)

# Folder setup
JOB_FOLDER = "/data/jobs"
QR_FOLDER = "/data/qr"
LOG_FOLDER = "/data/log"
PHOTO_FOLDER = "/data/photos"
for folder in [JOB_FOLDER, QR_FOLDER, LOG_FOLDER, PHOTO_FOLDER]:
    os.makedirs(folder, exist_ok=True)

now = datetime.now(ZoneInfo("Australia/Melbourne"))

@app.route("/")
def home():
    return redirect(url_for('upload'))

@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        loadsheet_no = request.form.get("loadsheet_no")
        job_no       = request.form.get("job_no")
        job_desc     = request.form.get("job_desc")
        driver       = request.form.get("driver")
        panels_text  = request.form.get("panels")
        panel_ids    = [p.strip() for p in panels_text.splitlines() if p.strip()]

        # Save job panels keyed by loadsheet
        panel_file = os.path.join(JOB_FOLDER, f"{loadsheet_no}.csv")
        pd.DataFrame({"PanelID": panel_ids}).to_csv(panel_file, index=False)

        # Log dispatch upload
        dispatch_log = os.path.join(LOG_FOLDER, "dispatch_log.csv")
        dispatch_entry = pd.DataFrame([{
            "Timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "Loadsheet No": loadsheet_no,
            "Job No":       job_no,
            "Description":  job_desc,
            "Driver":       driver,
            "Panel Count":  len(panel_ids),
            "Panel IDs":    ";".join(panel_ids)
        }])
        if os.path.exists(dispatch_log):
            dispatch_entry.to_csv(dispatch_log, mode="a", header=False, index=False)
        else:
            dispatch_entry.to_csv(dispatch_log, mode="w", header=True, index=False)

        # Generate QR code
        qr_url = url_for("scan", loadsheet=loadsheet_no, _external=True)
        qr_img = qrcode.make(qr_url)
        qr_path = os.path.join(QR_FOLDER, f"{loadsheet_no}.png")
        qr_img.save(qr_path)

        # Encode QR for display
        with open(qr_path, "rb") as f:
            qr_base64 = base64.b64encode(f.read()).decode('utf-8')

        return render_template(
            "upload.html",
            loadsheet_no=loadsheet_no,
            job_no=job_no,
            job_desc=job_desc,
            driver=driver,
            panel_count=len(panel_ids),
            qr_url=qr_url,
            qr_img=qr_base64,
            qr_download=url_for("download_qr", loadsheet=loadsheet_no)
        )
    return render_template("upload.html")

@app.route("/download_qr/<loadsheet>")
def download_qr(loadsheet):
    qr_path = os.path.join(QR_FOLDER, f"{loadsheet}.png")
    return send_file(qr_path, as_attachment=True)

@app.route("/scan")
def scan():
    loadsheet = request.args.get("loadsheet")
    panel_file = os.path.join(JOB_FOLDER, f"{loadsheet}.csv")
    dispatch_csv = os.path.join(LOG_FOLDER, "dispatch_log.csv")
    job_no = ""

    if os.path.exists(dispatch_csv):
        df = pd.read_csv(dispatch_csv, dtype=str)
        match = df[df["Loadsheet No"] == loadsheet]
        if not match.empty:
            job_no = match.iloc[0].get("Job No", "")
    if not os.path.exists(panel_file):
        return f"No loadsheet found for {loadsheet}", 404

    panels = pd.read_csv(panel_file)["PanelID"].tolist()
    return render_template("scan.html", loadsheet=loadsheet, panel_ids=panels, job_no=job_no)

@app.route("/submit_event", methods=["POST"])
def submit_event():
    loadsheet  = request.form.get("loadsheet")
    job_no     = request.form.get("job_no")
    event_type = request.form.get("event_type")
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    # Base log_data structure
    log_data = {
        "Timestamp":         timestamp,
        "Loadsheet No":      loadsheet,
        "Job No":            job_no,
        "Event":             event_type,
        "AP Staff":          "",
        "Photos":            "",
        "Delivery Outcome":  "",
        "Delivered Panels":  "",
        "Receiver Signature":"",
        "Failure Reason":    ""
    }

    # Helper to save a dataURL signature to disk
    def save_sig(data_url, folder, filename):
        if not data_url:
            return None
        header, b64 = data_url.split(",", 1)
        data = base64.b64decode(b64)
        sig_dir = os.path.join(PHOTO_FOLDER, loadsheet, folder)
        os.makedirs(sig_dir, exist_ok=True)
        path = os.path.join(sig_dir, filename)
        with open(path, "wb") as f:
            f.write(data)
        return filename

    # 1) Handle truck photos (unchanged)
    if event_type in ["leaving_ap", "arrived_site"]:
        files = request.files.getlist("photos")
        photo_dir = os.path.join(PHOTO_FOLDER, loadsheet, event_type)
        os.makedirs(photo_dir, exist_ok=True)
        saved = []
        for idx, file in enumerate(files):
            if file and file.filename:
                ext = os.path.splitext(file.filename)[1]
                name = f"{event_type}_{idx+1}_{timestamp.replace(':','-')}{ext}"
                file.save(os.path.join(photo_dir, name))
                saved.append(name)
        log_data["Photos"] = ";".join(saved)

    # 2) AP Staff: typed name + canvas signature
    if event_type == "leaving_ap":
        # typed name
        ap_name = request.form.get("ap_staff", "")
        log_data["AP Staff"] = ap_name

        # canvas signature
        sig_png = save_sig(request.form.get("ap_signature_img", ""),
                           "leaving_ap", f"ap_signature_{timestamp.replace(':','-')}.png")
        if sig_png:
            log_data["Photos"] += (";" if log_data["Photos"] else "") + sig_png

    # 3) Left-site delivery outcomes
    if event_type == "left_site":
        outcome = request.form.get("delivery_outcome", "")
        log_data["Delivery Outcome"] = outcome

        # Full delivery
        if outcome == "full":
            rec_name = request.form.get("receiver_signature_full", "")
            log_data["Receiver Signature"] = rec_name

            sig_png = save_sig(request.form.get("receiver_signature_full_img", ""),
                               "left_site", f"receiver_full_{timestamp.replace(':','-')}.png")
            if sig_png:
                log_data["Photos"] += (";" if log_data["Photos"] else "") + sig_png

        # Partial delivery
        elif outcome == "partial":
            panels = request.form.getlist("delivered_panels")
            log_data["Delivered Panels"]  = ";".join(panels)
            log_data["Failure Reason"]    = request.form.get("partial_reason", "")
            rec_name = request.form.get("receiver_signature_partial", "")
            log_data["Receiver Signature"] = rec_name

            sig_png = save_sig(request.form.get("receiver_signature_partial_img", ""),
                               "left_site", f"receiver_partial_{timestamp.replace(':','-')}.png")
            if sig_png:
                log_data["Photos"] += (";" if log_data["Photos"] else "") + sig_png

        # Failed delivery
        elif outcome == "failed":
            log_data["Failure Reason"]    = request.form.get("failure_reason", "")
            rec_name = request.form.get("receiver_signature_failed", "")
            log_data["Receiver Signature"] = rec_name

            sig_png = save_sig(request.form.get("receiver_signature_failed_img", ""),
                               "left_site", f"receiver_failed_{timestamp.replace(':','-')}.png")
            if sig_png:
                log_data["Photos"] += (";" if log_data["Photos"] else "") + sig_png

    # 4) Write the combined log_data to CSV
    event_log = os.path.join(LOG_FOLDER, f"{loadsheet}_events.csv")
    df_event = pd.DataFrame([log_data])
    if os.path.exists(event_log):
        df_event.to_csv(event_log, mode="a", header=False, index=False)
    else:
        df_event.to_csv(event_log, mode="w", header=True, index=False)

    return redirect(url_for('scan', loadsheet=loadsheet))

@app.route("/dashboard")
def dashboard():
    dispatch_csv = os.path.join(LOG_FOLDER, "dispatch_log.csv")
    records = []
    if os.path.exists(dispatch_csv):
        df = pd.read_csv(dispatch_csv, dtype=str).fillna('')
        records = df.to_dict('records')
    events = []
    for path in glob.glob(os.path.join(LOG_FOLDER, "*_events.csv")):
        ld = os.path.basename(path).replace("_events.csv", "")
        df = pd.read_csv(path, dtype=str).fillna('')
        for r in df.to_dict('records'):
            r['Loadsheet No'] = ld
            events.append(r)
    return render_template("dashboard.html", dispatch_records=records, event_records=events)

@app.route("/analysis", methods=["GET","POST"])
def analysis():
    result=None
    if request.method=="POST":
        ls = request.form.get("loadsheet","").strip()
        path=os.path.join(LOG_FOLDER,f"{ls}_events.csv")
        if os.path.exists(path):
            df=pd.read_csv(path, dtype=str).fillna('')
            df['ts']=pd.to_datetime(df['Timestamp'],errors='coerce')
            t1=df.loc[df['Event']=='leaving_ap','ts'].min()
            t2=df.loc[df['Event']=='arrived_site','ts'].min()
            t3=df.loc[df['Event']=='left_site','ts'].min()
            travel=(t2-t1) if pd.notnull(t1) and pd.notnull(t2) else None
            onsite=(t3-t2) if pd.notnull(t2) and pd.notnull(t3) else None
            bc=0.0
            if onsite:
                extra=max(0,onsite.total_seconds()-2*3600)
                bc=round((extra/3600)*200,2)
            result={
                'loadsheet':ls,'job_no':df['Job No'].iloc[0],'left_ap':t1,'arrived':t2,'left_site':t3,'travel':travel,'onsite':onsite,'back_charge':bc
            }
        else:
            result={'error':f"No event log for {ls}"}
    return render_template("analysis.html",result=result)

@app.route("/photos/<loadsheet>/<event>/<filename>")
def get_photo(loadsheet,event,filename):
    path=os.path.join(PHOTO_FOLDER,loadsheet,event,filename)
    return send_file(path)

import os
import tempfile
import pandas as pd
from flask import send_file, current_app
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors

@app.route("/generate_docket/<loadsheet>")
def generate_docket(loadsheet):
    dispatch_csv = os.path.join(LOG_FOLDER, "dispatch_log.csv")
    event_csv    = os.path.join(LOG_FOLDER, f"{loadsheet}_events.csv")

    # 1) Check logs exist
    if not os.path.exists(dispatch_csv) or not os.path.exists(event_csv):
        current_app.logger.error(f"Missing data for {loadsheet}: {dispatch_csv} or {event_csv}")
        return f"Missing data for {loadsheet}", 404

    # 2) Load data
    dispatch_df = pd.read_csv(dispatch_csv, dtype=str).fillna("")
    event_df    = pd.read_csv(event_csv,    dtype=str).fillna("")
    dispatch_row = dispatch_df.loc[dispatch_df["Loadsheet No"] == loadsheet].iloc[0]

    # 3) Create temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    pdf_path = tmp.name
    tmp.close()
    current_app.logger.info(f"Generating PDF at {pdf_path}")

    # 4) Start PDF
    c = canvas.Canvas(pdf_path, pagesize=A4)
    width, height = A4

    # — Header —
    logo_path = os.path.join("static", "logo.jpg")
    if os.path.exists(logo_path):
        c.drawImage(ImageReader(logo_path), 50, height - 80, width=100,
                    preserveAspectRatio=True, mask='auto')
    c.setFillColor(colors.HexColor("#004080"))
    c.rect(0, height - 100, width, 30, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(200, height - 90, "Proof of Delivery")
    c.setFillColor(colors.black)

    # — Job Information —
    c.setFont("Helvetica-Bold", 11)
    y = height - 130
    c.drawString(50, y, "Job Information")
    c.line(50, y - 2, width - 50, y - 2)
    c.setFont("Helvetica", 10)
    y -= 20
    c.drawString(50, y, f"Loadsheet No: {loadsheet}")
    c.drawString(300, y, f"Date: {dispatch_row['Timestamp']}")
    y -= 15
    c.drawString(50, y, f"Job No: {dispatch_row['Job No']}")
    c.drawString(300, y, f"Driver: {dispatch_row['Driver']}")
    y -= 15
    c.drawString(50, y, f"Panel Count: {dispatch_row['Panel Count']}")
    y -= 15
    c.drawString(50, y, f"Panels: {dispatch_row['Panel IDs']}")
    y -= 20

    # — Events & Images —
    full_panel_list = dispatch_row['Panel IDs'].split(";")
    event_titles = {
        "leaving_ap":   "LEAVING ADVANCED PRECAST",
        "arrived_site": "ARRIVED AT SITE",
        "left_site":    "LEFT SITE"
    }

    for event_type in ["leaving_ap", "arrived_site", "left_site"]:
        ev_df = event_df[event_df["Event"] == event_type]
        if ev_df.empty:
            continue

        # New page if needed
        if y < 120:
            c.showPage()
            y = height - 80

        # Section header
        c.setFont("Helvetica-Bold", 11)
        c.drawString(50, y, event_titles[event_type])
        y -= 4
        c.line(50, y, width - 50, y)
        y -= 16
        c.setFont("Helvetica", 9)

        event_dir = os.path.join(PHOTO_FOLDER, loadsheet, event_type)
        current_app.logger.info(f"Looking for images in {event_dir}")
        if os.path.isdir(event_dir):
            for fn in sorted(os.listdir(event_dir)):
                current_app.logger.info(f"  Found file: {fn}")

        for _, row in ev_df.iterrows():
            if y < 120:
                c.showPage()
                y = height - 80

            # Timestamp
            c.drawString(50, y, f"Timestamp: {row['Timestamp']}")
            y -= 12

            # AP staff signature
            if event_type == "leaving_ap" and row.get("AP Staff"):
                c.drawString(50, y, f"AP Staff: {row['AP Staff']}")
                y -= 12
                if os.path.isdir(event_dir):
                    for fn in os.listdir(event_dir):
                        if fn.startswith("ap_signature_"):
                            path = os.path.join(event_dir, fn)
                            c.drawImage(ImageReader(path), 50, y-60, width=200, height=50,
                                        preserveAspectRatio=True, mask='auto')
                            y -= 70
                            break

            # Left-site details
            if event_type == "left_site" and row.get("Delivery Outcome"):
                c.drawString(50, y, f"Outcome: {row['Delivery Outcome']}")
                y -= 12
                if row.get("Receiver Signature"):
                    c.drawString(50, y, f"Receiver: {row['Receiver Signature']}")
                    y -= 12
                    if os.path.isdir(event_dir):
                        for fn in os.listdir(event_dir):
                            if fn.startswith("receiver_"):
                                path = os.path.join(event_dir, fn)
                                c.drawImage(ImageReader(path), 50, y-60, width=200, height=50,
                                            preserveAspectRatio=True, mask='auto')
                                y -= 70
                                break
                if row.get("Failure Reason"):
                    c.drawString(50, y, f"Reason: {row['Failure Reason']}")
                    y -= 12
                if row.get("Delivered Panels"):
                    delivered = row['Delivered Panels'].split(";")
                    undelivered = [p for p in full_panel_list if p not in delivered]
                    c.drawString(50, y, f"Delivered Panels: {', '.join(delivered)}")
                    y -= 12
                    c.drawString(50, y, f"Undelivered: {', '.join(undelivered)}")
                    y -= 12

            # Truck photos
            photos = [fn for fn in row.get("Photos", "").split(";") if fn]
            if os.path.isdir(event_dir):
                for fn in photos:
                    if fn.startswith("ap_signature_") or fn.startswith("receiver_"):
                        continue
                    pth = os.path.join(event_dir, fn)
                    if os.path.exists(pth):
                        try:
                            c.drawImage(ImageReader(pth), 50, y-100, width=120, height=90,
                                        preserveAspectRatio=True, mask='auto')
                            c.setFont("Helvetica-Oblique", 7)
                            c.drawString(50, y-110, fn)
                            y -= 120
                        except Exception as e:
                            current_app.logger.error(f"Failed to draw {pth}: {e}")
                            c.setFont("Helvetica", 8)
                            c.drawString(50, y, f"Could not load {fn}")
                            y -= 15

            y -= 6

    # — Footer —
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(colors.gray)
    c.drawString(50, 40, "Generated by Advanced Precast Transport App")

    c.save()

    # 6) Log file size
    size = os.path.getsize(pdf_path)
    current_app.logger.info(f"PDF saved: {size} bytes")

    # 7) Stream and clean up
    response = send_file(
        pdf_path,
        download_name=f"docket_{loadsheet}.pdf",
        as_attachment=True,
        mimetype="application/pdf"
    )
    @response.call_on_close
    def cleanup():
        try:
            os.remove(pdf_path)
        except OSError:
            pass

    return response


@app.route("/edit_event", methods=["GET", "POST"])
def edit_event():
    loadsheet = request.values.get("loadsheet")
    ts        = request.values.get("timestamp")
    event_log = os.path.join(LOG_FOLDER, f"{loadsheet}_events.csv")
    if not os.path.exists(event_log):
        return "No such event log", 404

    # load into DataFrame
    df = pd.read_csv(event_log, dtype=str)
    # find the row by exact timestamp match
    mask = df["Timestamp"] == ts
    if not mask.any():
        return "Entry not found", 404

    if request.method == "POST":
        # update fields from form
        df.loc[mask, "AP Staff"]           = request.form.get("ap_staff", "")
        df.loc[mask, "Delivery Outcome"]   = request.form.get("delivery_outcome", "")
        df.loc[mask, "Receiver Signature"] = request.form.get("receiver_signature", "")
        df.loc[mask, "Failure Reason"]     = request.form.get("failure_reason", "")
        df.loc[mask, "Delivered Panels"]   = request.form.get("delivered_panels", "")
        # overwrite CSV
        df.to_csv(event_log, index=False)
        return redirect(url_for("dashboard"))

    # GET: show a simple edit form
    entry = df[mask].iloc[0]
    return render_template("edit_event.html", loadsheet=loadsheet, entry=entry)

@app.route("/delete_event", methods=["POST"])
def delete_event():
    loadsheet = request.form.get("loadsheet")
    ts        = request.form.get("timestamp")
    event_log = os.path.join(LOG_FOLDER, f"{loadsheet}_events.csv")
    if not os.path.exists(event_log):
        return "No such event log", 404

    df = pd.read_csv(event_log, dtype=str)
    df = df[df["Timestamp"] != ts]  # remove that row
    df.to_csv(event_log, index=False)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(debug=True)
