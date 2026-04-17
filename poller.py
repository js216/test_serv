import time
import ft4222
import serial
import urllib.request
import hashlib
from datetime import datetime
import pyvisa
import csv
import io
import numpy as np


class Remote:
    def __init__(self, port=8080):
        self.port = port

    def get_job(self, kind):
        r = urllib.request.urlopen(f"http://localhost:{self.port}/{kind}")
        if r.status == 204:
            return None, {}
        return r.read(), dict(r.headers)

    def post_resp(self, job_id, msg, suffix=""):

        if isinstance(msg, str):
            msg_bytes = msg.encode()
        else:
            msg_bytes = msg

        if suffix:
            url = f"http://localhost:{self.port}/{job_id}{suffix}"
        else:
            url = f"http://localhost:{self.port}/{job_id}"

        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=msg_bytes,
                method="POST"
            )
        ).read()


class Expander:
    """
    ADP5587ACPZ-1 pinout
    =====================================
    C0 = USBI_SPI0_EN*
    C1 = USBI_SPI1_EN*
    C2 = USB_QSPI_EN*
    C3 = USB_QSPI_RESET*
    C4 = ETH0_RESET
    C5 = ADAU1372_PWRDWN*
    C6 = PUSHBUTTON_EN
    C7 = DS8 (green LED on SOMCRR)
    C8 = DS7 (green LED on SOMCRR)
    C9 = DS6 (green LED on SOMCRR)
    """

    def wr(self, dev, d):
        for a, b, c in d:
            dev.i2cMaster_WriteEx(
                a, ft4222.I2CMaster.Flag.START_AND_STOP, bytes([b, c]))

    def exp_init(self, ):
        dev = ft4222.openByDescription('FT4222 A')
        dev.i2cMaster_Init(100)

        self.wr(dev, [(0x30, 0x1D, 0x00)]) # R7:0 -> GPIO
        self.wr(dev, [(0x30, 0x1E, 0x00)]) # C7:0 -> GPIO
        self.wr(dev, [(0x30, 0x1F, 0x00)]) # C9:8 -> GPIO

        # `USBI_SPI0,1` disabled, `USB_QSPI` neither enabled nor under reset,
        # `ETH0` reset asserted, `ADAU1372` powered down, pushbutton disabled,
        # LEDs off.
        self.wr(dev, [(0x30, 0x18, 0b1001_1111)])
        self.wr(dev, [(0x30, 0x19, 0b0000_0011)])

        self.wr(dev, [(0x30, 0x24, 0xff)]) # C7:0 -> output
        self.wr(dev, [(0x30, 0x25, 0xff)]) # C9:8 -> output

        self.wr(dev, [(0x30, 0x18, 0b1001_1011)]) # enable QSPI

        dev.close()
        
    def blink_led(self, led):
        dev = ft4222.openByDescription('FT4222 A')
        dev.i2cMaster_Init(100)

        if led == "DS8":
            self.wr(dev, [(0x30, 0x18, 0b0001_1011)])
        elif led == "DS7":
            self.wr(dev, [(0x30, 0x19, 0b0000_0010)])
        elif led == "DS6":
            self.wr(dev, [(0x30, 0x19, 0b0000_0001)])

        time.sleep(0.25)

        if led == "DS8":
            self.wr(dev, [(0x30, 0x18, 0b1001_1111)])
        elif led == "DS7":
            self.wr(dev, [(0x30, 0x19, 0b0000_0011)])
        elif led == "DS6":
            self.wr(dev, [(0x30, 0x19, 0b0000_0011)])

        dev.close()
        
    def reset(self):
        # LED DS8 repurposed as eval board reset control
        self.blink_led("DS8")


class QSPI:
    def load_file(self, fname):
        boot_buffer = bytearray([0x03])
        with open(fname, 'r') as f:
            for line in f:
                boot_buffer.append(int(line.strip(), 16))
        self.load_stream(boot_buffer)

    def accept_ldr(self, ldr):
        boot_buffer = bytearray([0x03])
        for line in ldr.split():
            boot_buffer.append(int(line.strip(), 16))
        self.load_stream(boot_buffer)

    def _write_chunked(self, dev, buf):
        CHUNK = 1024

        # pad to multiple of 1024
        n = len(buf)
        padded_len = ((n + CHUNK - 1) // CHUNK) * CHUNK
        if padded_len != n:
            buf += bytes(padded_len - n)

        # keep CS LOW across entire transfer
        for i in range(0, padded_len, CHUNK):
            last = (i + CHUNK) >= padded_len
            dev.spiMaster_SingleWrite(buf[i:i+CHUNK], last)

    def load_stream(self, boot_buffer):
        dev = ft4222.openByDescription('FT4222 A')
        dev.spiMaster_Init(
            ft4222.SPIMaster.Mode.SINGLE,
            ft4222.SPIMaster.Clock.DIV_8,
            ft4222.SPI.Cpol.IDLE_LOW,
            ft4222.SPI.Cpha.CLK_TRAILING,
            ft4222.SPIMaster.SlaveSelect.SS0)

        self._write_chunked(dev, boot_buffer)

        dev.close()


class ScopeDriver:
    def __init__(self, inst):
        self.inst = inst

    def _parse_val(self, s):
        return float(''.join(c for c in s if (c.isdigit() or c in ".-+eE")))

    def configure(self):
        self.inst.write("STOP")
        self.inst.write("WAV:FORM BYTE")
        self.inst.write("WAV:MODE RAW")
        self.inst.write("WAV:POIN MAX")

    def read_channel(self, ch):
        self.inst.write(f"WAV:SOUR {ch}")

        raw = self.inst.query_binary_values(
            "WAV:DATA?",
            datatype="B",
            container=bytes
        )

        wave = np.frombuffer(raw, dtype=np.uint8)

        vdiv = self._parse_val(self.inst.query(f"{ch}:VDIV?"))
        ofst = self._parse_val(self.inst.query(f"{ch}:OFST?"))
        sara = self._parse_val(self.inst.query("SARA?"))

        v = (wave - 128) * (vdiv / 25.0) - ofst
        t = np.arange(len(v)) / sara

        return t, v

    def get_traces(self, channels):
        self.configure()

        out = {}
        for ch in channels:
            out[ch] = self.read_channel(ch)

        self.inst.write("RUN")
        self.inst.write("TRIG:MODE AUTO")

        return out

    def traces_to_csv(self, traces, max_points=5000):
        first_ch = next(iter(traces))
        t = traces[first_ch][0]

        channels = list(traces.keys())
        vs = [traces[ch][1] for ch in channels]

        n = min([len(t)] + [len(v) for v in vs])
        step = max(1, n // max_points)

        t = t[::step]
        vs = [v[::step] for v in vs]

        buf = io.StringIO()
        w = csv.writer(buf)

        w.writerow(["t"] + channels)

        for i in range(len(t)):
            w.writerow([t[i]] + [vs[j][i] for j in range(len(channels))])

        return buf.getvalue()


class JobHandler:
    """Process a single job payload and return its artefacts.

    Subclasses implement run(payload, headers) and return a dict
    {suffix: bytes|str}, where each key is a file extension including
    the leading dot (e.g. ".csv", ".txt"). The Poller POSTs ".txt"
    last, so handlers don't need to think about artefact ordering.
    """
    def run(self, payload, headers):
        raise NotImplementedError


class NullHandler(JobHandler):
    """Acknowledge a job and return canned artefacts.

    Useful for exercising the dispatch pipeline (and the harness
    protocol end-to-end) before real hardware is wired up for a new
    kind.
    """
    def run(self, payload, headers):
        txt = (
            f"null handler: received {len(payload)} bytes\n"
            f"headers: {dict(headers)}\n"
            "OK fake_test_1\n"
            "OK fake_test_2\n"
        )
        csv = "t,ch1,ch2\n0.0,0.0,1.0\n1.0,0.5,0.5\n2.0,1.0,0.0\n"
        return {".csv": csv, ".txt": txt}


class DspHandler(JobHandler):
    def __init__(self,
                 serial_port="COM31",
                 baudrate=115200,
                 rx_duration_s=5,
                 scope_resource=None):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.rx_duration_s = rx_duration_s

        self.expander = Expander()
        self.qspi = QSPI()

        if scope_resource is not None:
            rm = pyvisa.ResourceManager()
            inst = rm.open_resource(scope_resource)
            self.scope = ScopeDriver(inst)
        else:
            self.scope = None

    def _read_serial_for(self, ser, duration_s):
        start = time.time()
        buf = []

        while time.time() - start < duration_s:
            data = ser.read(1024)
            if data:
                buf.append(data)

        return b"".join(buf).decode(errors="replace")

    def run(self, payload, headers):
        # Open the UART before loading so the first byte of the new
        # firmware's output is captured. Reset halts the old firmware;
        # drain clears any bytes it emitted before reset took effect.
        ser = serial.Serial(
            self.serial_port,
            baudrate=self.baudrate,
            timeout=0.1
        )
        try:
            self.expander.reset()
            self.expander.exp_init()
            ser.reset_input_buffer()

            self.qspi.accept_ldr(payload)

            duration = float(headers.get("X-Test-Runtime", self.rx_duration_s))
            print(datetime.now(), f"runtime={duration}s")
            uart_msg = self._read_serial_for(ser, duration)
        finally:
            ser.close()

        artefacts = {}
        if self.scope is not None:
            traces = self.scope.get_traces(("C1", "C2", "C3", "C4"))
            artefacts[".csv"] = self.scope.traces_to_csv(traces)
        artefacts[".txt"] = uart_msg
        return artefacts


class Poller:
    def __init__(self, handlers, http_port=8080, poll_interval_s=1.0):
        self.handlers = handlers
        self.poll_interval_s = poll_interval_s
        self.remote = Remote(http_port)
        self._stop = False

    def stop(self):
        self._stop = True

    def _dispatch(self, kind, payload, headers):
        job_id = hashlib.sha256(payload).hexdigest()
        print(datetime.now(), kind, job_id)

        artefacts = self.handlers[kind].run(payload, headers)

        # .txt is the completion sentinel -- every other artefact must be
        # on disk by the time submit.py sees it, so POST it last.
        txt = artefacts.pop(".txt", b"")
        for ext, data in artefacts.items():
            self.remote.post_resp(job_id, data, suffix=ext)
        self.remote.post_resp(job_id, txt)

    def run(self):
        try:
            while not self._stop:
                for kind in self.handlers:
                    payload, headers = self.remote.get_job(kind)
                    if payload:
                        self._dispatch(kind, payload, headers)

                time.sleep(self.poll_interval_s)

        except KeyboardInterrupt:
            self._stop = True


def main():
    while True:
        try:
            handlers = {
                "ldr": DspHandler(
                    serial_port="COM31",
                    baudrate=115200,
                    rx_duration_s=5,
                    scope_resource="USB0::0xF4EC::0x1011::SDS2PDDX6R1848::INSTR",
                ),
                "hex": NullHandler(),
            }
            Poller(
                handlers=handlers,
                http_port=8080,
                poll_interval_s=2.5,
            ).run()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(datetime.now(), f"{type(e).__name__}: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
