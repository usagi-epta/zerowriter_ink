#!/usr/bin/env python3
"""
Zerowriter Ink Firmware Restore

"""

import contextlib
import io
import platform
import queue
import re
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

FIXED_CHIP = "esp32"
FIXED_BAUD = "460800"
FLASH_OFFSET = "0x0000"

DEFAULT_FW_NAME = "zw_latest.merged.bin"
PERCENT_RE = re.compile(r"\((\d+)\s*%\)")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:

    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_resource_dir() -> Path:

    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        return Path(mp).resolve()
    return app_dir()

# -----------------------------
def candidate_ports():
    if platform.system().lower().startswith("win"):
        return [f"COM{i}" for i in range(1, 51)]

    dev = Path("/dev")
    if not dev.exists():
        return []

    pats = [
        "cu.usbserial*",
        "cu.usbmodem*",
        "cu.SLAB_USBtoUART*",
        "cu.wchusbserial*",
        "tty.usbserial*",
        "tty.usbmodem*",
        "tty.SLAB_USBtoUART*",
        "tty.wchusbserial*",
        "ttyUSB*",
        "ttyACM*",
    ]

    out = []
    for pat in pats:
        out += [str(p) for p in dev.glob(pat)]

    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            uniq.append(p)
            seen.add(p)
    return uniq


def looks_like_binary(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 1024:
            return False
        head = path.read_bytes()[:256].lower()
        if b"<html" in head or b"<!doctype html" in head:
            return False
        return True
    except Exception:
        return False


# -----------------------------
def esptool_available() -> bool:
    try:
        import esptool 
        import serial  
        return True
    except Exception:
        return False


class QueueWriter(io.TextIOBase):
    def __init__(self, q: queue.Queue):
        self.q = q
        self._buf = ""

    def write(self, s):
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.q.put(line)
        return len(s)

    def flush(self):
        if self._buf:
            self.q.put(self._buf)
            self._buf = ""


def run_esptool_inproc(args, out_q: queue.Queue) -> int:

    try:
        import esptool
    except Exception as e:
        out_q.put(f"ERROR: esptool import failed: {e}")
        return 1

    writer = QueueWriter(out_q)

    try:
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            try:
                esptool.main(args)
                writer.flush()
                return 0
            except SystemExit as se:
                writer.flush()
                code = se.code
                if code is None:
                    return 0
                if isinstance(code, int):
                    return code
                return 1
    except Exception as e:
        out_q.put(f"ERROR: esptool runtime exception: {e}")
        return 1

# -----------------------------
class UpdaterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Zerowriter Ink Firmware Restore")
        self.geometry("900x650")
        self.minsize(860, 600)

        self.port_var = tk.StringVar(value="")
        self.fw_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready.")

        self._q = None
        self._pump_job = None
        self._saw_percent = False

        self._build_ui()
        self.refresh_ports(initial=True)
        self.set_default_firmware()

    # ---------- UI ----------
    def _build_ui(self):
        pad = 12
        root = ttk.Frame(self, padding=pad)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="Zerowriter Ink Firmware Restore", font=("Helvetica", 16, "bold")).pack(anchor="w")
        ttk.Label(root, text="This tool flashes a merged firmware image to Zerowriter Ink.").pack(anchor="w", pady=(2, 10))

        nb = ttk.Notebook(root)
        nb.pack(fill="x", pady=(0, 10))

        instr = ttk.Frame(nb, padding=10)
        nb.add(instr, text="Instructions")

        instructions_text = (
            "Important: To update with this tool, you must open your Zerowriter Ink and unplug the keyboard cable "
            "from the display board (the cable on the left side of the screen). The keyboard itself blocks the update "
            "signal from your computer, so it must be disconnected temporarily. To remove the keyboard cable, you can "
            "carefully wiggle it side-to-side until it releases.\n\n"
            "Back up your work: This update tool does NOT affect anything on the Zerowriter Ink SD card, "
            "but you should always back up your work before doing any update. Better safe than sorry.\n\n"
            "1. Turn off your Zerowriter Ink\n"
            "2. Unscrew the back panel to open your Zerowriter Ink\n"
            "3. Carefully unplug the keyboard connection cable on the left side of the display (black cable, white cap/connector)\n"
            "4. Connect your Zerowriter Ink to your computer via USB-C cable through the charging port.\n"
            "5. Turn ON your Zerowriter Ink from the power switch\n"
            "6. Run this updater: select your binary and press \"Update\""
        )

        txt = tk.Text(instr, height=16, wrap="word")
        txt.insert("1.0", instructions_text)
        txt.configure(state="disabled")
        txt.pack(fill="x", expand=False)

        controls = ttk.Frame(root)
        controls.pack(fill="x", pady=(6, 8))

        port_row = ttk.Frame(controls)
        port_row.pack(fill="x", pady=(0, 8))
        ttk.Label(port_row, text="Serial Port:").pack(side="left")

        self.port_combo = ttk.Combobox(port_row, textvariable=self.port_var, width=55, values=[])
        self.port_combo.pack(side="left", padx=(8, 8), fill="x", expand=True)

        ttk.Button(port_row, text="Refresh", command=self.refresh_ports).pack(side="left")

        fw_row = ttk.Frame(controls)
        fw_row.pack(fill="x", pady=(0, 8))
        ttk.Label(fw_row, text="Firmware (.merged.bin):").pack(side="left")

        self.fw_entry = ttk.Entry(fw_row, textvariable=self.fw_var)
        self.fw_entry.pack(side="left", padx=(8, 8), fill="x", expand=True)

        ttk.Button(fw_row, text="Browse…", command=self.browse_firmware).pack(side="left")
        ttk.Button(fw_row, text=f"Use {DEFAULT_FW_NAME}", command=self.use_default_firmware).pack(side="left", padx=(8, 0))

        action_row = ttk.Frame(root)
        action_row.pack(fill="x", pady=(4, 8))

        self.progress = ttk.Progressbar(action_row, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)

        self.update_btn = ttk.Button(action_row, text="Update", command=self.start_update)
        self.update_btn.pack(side="left", padx=(10, 0))

        ttk.Label(root, textvariable=self.status_var).pack(anchor="w")

        ttk.Label(root, text="Log:").pack(anchor="w", pady=(8, 0))
        self.log = tk.Text(root, height=8, wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.configure(font=("Menlo", 11) if platform.system() == "Darwin" else ("Consolas", 10))

        tech = ttk.Frame(root)
        tech.pack(fill="x", pady=(6, 0))
        ttk.Label(
            tech,
            text=f"Chip: {FIXED_CHIP}   Baud: {FIXED_BAUD}   Offset: {FLASH_OFFSET}",
            foreground="#555",
        ).pack(anchor="w")

    # ---------- helpers ----------
    def append_log(self, s: str):
        self.log.insert("end", s)
        self.log.see("end")
        self.update_idletasks()

    def refresh_ports(self, initial: bool = False):
        ports = candidate_ports()
        self.port_combo["values"] = ports

        if not ports:
            self.port_var.set("")
            if not initial:
                messagebox.showwarning(
                    "No ports",
                    "No serial ports detected.\n\nPlug in your Zerowriter Ink and try Refresh."
                )
            return

        current = self.port_var.get()
        if len(ports) == 1:
            self.port_var.set(ports[0])
        else:
            if current in ports:
                self.port_var.set(current)
            else:
                cu = [p for p in ports if "/cu." in p]
                self.port_var.set(cu[0] if cu else ports[0])

    def set_default_firmware(self):
        candidates = [
            app_dir() / DEFAULT_FW_NAME,
            bundled_resource_dir() / DEFAULT_FW_NAME,
            Path(__file__).resolve().parent / DEFAULT_FW_NAME,
        ]
        for c in candidates:
            if c.exists():
                self.fw_var.set(str(c.resolve()))
                return

    def use_default_firmware(self):
        self.set_default_firmware()
        p = Path(self.fw_var.get())
        if p.exists():
            messagebox.showinfo("Firmware", f"Selected:\n{p.name}")
        else:
            messagebox.showwarning("Firmware", f"Not found:\n{p}")

    def browse_firmware(self):
        path = filedialog.askopenfilename(
            title="Select merged firmware (.bin)",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if path:
            self.fw_var.set(path)

    def validate_inputs(self) -> tuple[bool, str]:
        if not esptool_available():
            return False, (
                "This app is missing bundled dependencies (esptool/pyserial).\n\n"
                "This is a build issue — please download the correct updater."
            )

        port = self.port_var.get().strip()
        if not port:
            return False, "No serial port selected."

        fw = self.fw_var.get().strip()
        if not fw:
            return False, "No firmware file selected."

        p = Path(fw).expanduser()
        if not p.exists():
            return False, f"Firmware not found:\n{p}"
        if not looks_like_binary(p):
            return False, f"File doesn’t look like a valid firmware binary:\n{p}"

        return True, ""

    # ---------- flashing ----------
    def start_update(self):
        ok, msg = self.validate_inputs()
        if not ok:
            messagebox.showerror("Cannot update", msg)
            return

        self.update_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._saw_percent = False
        self.status_var.set("Updating… do not unplug the device.")
        self.append_log("\n=== UPDATE START ===\n")

        self._q = queue.Queue()

        port = self.port_var.get().strip()
        fw = str(Path(self.fw_var.get()).expanduser().resolve())

        args = [
            "--chip", FIXED_CHIP,
            "-p", port,
            "-b", FIXED_BAUD,
            "write_flash",
            FLASH_OFFSET,
            fw,
        ]

        self.append_log("Command:\n  esptool " + " ".join(args) + "\n\n")

        def worker():
            rc = run_esptool_inproc(args, self._q)
            self._q.put(("__DONE__", rc))

        threading.Thread(target=worker, daemon=True).start()

        self._pump_queue()

    def _pump_queue(self):
        if not self._q:
            return

        try:
            while True:
                msg = self._q.get_nowait()

                if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "__DONE__":
                    rc = msg[1]
                    if rc == 0:
                        self._finish_ok()
                    else:
                        self._finish_fail()
                    self._q = None
                    return

                line = str(msg).rstrip("\r")
                if line:
                    self.append_log(line + "\n")
                    self._handle_progress_and_status(line)
        except queue.Empty:
            pass

        self._pump_job = self.after(50, self._pump_queue)

    def _handle_progress_and_status(self, line: str):
        m = PERCENT_RE.search(line)
        if m:
            try:
                pct = int(m.group(1))
                pct = max(0, min(100, pct))
                self._saw_percent = True
                self._set_percent(pct)
            except Exception:
                pass

        low = line.lower()
        if "connecting" in low:
            self.status_var.set("Connecting…")
        elif "writing at" in low or "wrote" in low:
            self.status_var.set("Writing firmware…")
        elif "hash of data verified" in low or "verified" in low:
            self.status_var.set("Verifying…")

    def _set_percent(self, pct: int):
        if str(self.progress["mode"]) != "determinate":
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=100)
        self.progress["value"] = pct

    def _finish_ok(self):
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100)
        self.progress["value"] = 100
        self.update_btn.configure(state="normal")
        self.status_var.set("Update complete.")
        self.append_log("\n=== UPDATE COMPLETE ===\n")
        messagebox.showinfo("Success", "Update complete.")

    def _finish_fail(self):
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100)
        self.progress["value"] = 0
        self.update_btn.configure(state="normal")
        self.status_var.set("Update failed.")
        self.append_log("\n=== UPDATE FAILED ===\n")
        messagebox.showerror(
            "Update failed",
            "Update failed.\n\nCommon fixes:\n"
            "- Unplug/replug USB, try a different cable\n"
            "- Close any serial monitor using the port\n"
            "- Ensure the keyboard cable is unplugged (per instructions)\n"
        )


def main():
    app = UpdaterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
