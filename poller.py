import sys
import time
import ft4222
import serial
import serial.tools.list_ports
import urllib.request
import hashlib
from datetime import datetime
import pyvisa
import csv
import io
import numpy as np
import ftd2xx


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


class Icestick:
    """Program iCEstick SPI flash (Micron N25Q032) via FT2232H channel A,
    then release CRESET so the FPGA boots from flash.

    Drives the FT2232H in MPSSE mode through FTDI's D2XX driver (via
    ftd2xx), so this coexists with the VCP driver on channel B and
    needs no Zadig/WinUSB binding.

    FT2232H ADBUS (channel A) pinout on the iCEstick
    (per the iceprog reference in Project IceStorm):
        AD0 = SCK       (out)
        AD1 = MOSI      (out, to flash SI)
        AD2 = MISO      (in,  from flash SO)
        AD4 = CS*       (out, to flash CS*)
        AD6 = CDONE     (in,  FPGA config done)
        AD7 = CRESET*   (out, active low FPGA reset)
    """
    DESC = b"Dual RS232-HS A"   # FT2232H channel A, under FTDI driver
    CLOCK_DIVISOR = 29          # 60 MHz / ((1+29)*2) = 1 MHz SPI
                                # (slow while bringing up; bump later)

    # Direction byte for ADBUS: AD0,1,4,7 outputs; AD2,3,5,6 inputs.
    DIR_LOW = 0x93
    # Output values (applied via MPSSE cmd 0x80).
    #   bit 4 = CS*    (1 = de-asserted)
    #   bit 7 = CRESET*(1 = FPGA running, 0 = FPGA held in reset)
    IDLE_RESET    = 0x10   # CS=1, CRESET=0
    IDLE_RUN      = 0x90   # CS=1, CRESET=1
    ACTIVE_RESET  = 0x00   # CS=0, CRESET=0

    PAGE = 256
    SECTOR = 64 * 1024

    def program(self, bitstream):
        dev = ftd2xx.openEx(self.DESC, 2)   # 2 = OPEN_BY_DESCRIPTION
        try:
            self._mpsse_init(dev)
            self._set_low(dev, self.IDLE_RESET)  # hold FPGA in reset
            time.sleep(0.01)
            self._cmd(dev, [0xAB])               # wake flash
            time.sleep(0.001)                    # tRES1 (~30 us) slack

            ident = self._xfer(dev, [0x9F], 3)
            if ident[0] != 0x20:   # Micron JEDEC manufacturer code
                raise RuntimeError(f"bad flash ID: {ident.hex()}")

            self._erase(dev, len(bitstream))
            self._write(dev, bitstream)
            self._verify(dev, bitstream)

            self._set_low(dev, self.IDLE_RUN)    # release FPGA
        finally:
            dev.setBitMode(0, 0)   # back to reset mode; pins tri-state
            dev.close()

    def _mpsse_init(self, dev):
        dev.setTimeouts(3000, 3000)
        dev.setLatencyTimer(1)
        dev.resetDevice()
        dev.purge(3)               # flush RX+TX
        dev.setBitMode(0, 0x00)    # reset bit mode
        time.sleep(0.02)
        dev.setBitMode(0, 0x02)    # enter MPSSE
        time.sleep(0.02)
        dev.purge(3)

        # Sync check: bogus opcode 0xAA should elicit "0xFA 0xAA".
        dev.write(bytes([0xAA]))
        time.sleep(0.05)
        resp = dev.read(2)
        if resp != b"\xFA\xAA":
            raise RuntimeError(
                f"MPSSE sync failed: expected 0xFA 0xAA, got {resp.hex()}"
            )

        dev.write(bytes([
            0x85,                  # disable loopback
            0x8A,                  # disable clock div-by-5 (60 MHz base)
            0x97,                  # disable adaptive clocking
            0x8D,                  # disable 3-phase clocking
            0x86,
            self.CLOCK_DIVISOR        & 0xff,
            (self.CLOCK_DIVISOR >> 8) & 0xff,
        ]))
        time.sleep(0.01)

    def _set_low(self, dev, value):
        dev.write(bytes([0x80, value, self.DIR_LOW]))

    def _cmd(self, dev, out):
        """Write `out` as one CS-asserted SPI transaction (no read)."""
        n = len(out) - 1
        dev.write(
            bytes([0x80, self.ACTIVE_RESET, self.DIR_LOW,
                   0x11, n & 0xff, (n >> 8) & 0xff])
            + bytes(out)
            + bytes([0x80, self.IDLE_RESET, self.DIR_LOW])
        )

    def _xfer(self, dev, out, rdlen):
        """Write `out`, then clock in `rdlen` bytes, under one CS."""
        nw = len(out) - 1
        nr = rdlen - 1
        dev.write(
            bytes([0x80, self.ACTIVE_RESET, self.DIR_LOW,
                   0x11, nw & 0xff, (nw >> 8) & 0xff])
            + bytes(out)
            + bytes([0x20, nr & 0xff, (nr >> 8) & 0xff,
                     0x80, self.IDLE_RESET, self.DIR_LOW,
                     0x87])
        )
        got = dev.read(rdlen)
        if len(got) != rdlen:
            raise RuntimeError(
                f"short read: got {len(got)} of {rdlen} bytes"
            )
        return got

    def _wait_wip(self, dev):
        while self._xfer(dev, [0x05], 1)[0] & 0x01:
            time.sleep(0.001)

    def _erase(self, dev, nbytes):
        for addr in range(0, nbytes, self.SECTOR):
            self._cmd(dev, [0x06])   # WREN
            self._cmd(dev, [0xD8,
                            (addr >> 16) & 0xff,
                            (addr >>  8) & 0xff,
                             addr        & 0xff])
            self._wait_wip(dev)

    def _write(self, dev, buf):
        for off in range(0, len(buf), self.PAGE):
            chunk = bytes(buf[off:off + self.PAGE])
            self._cmd(dev, [0x06])   # WREN
            self._cmd(dev, [0x02,
                            (off >> 16) & 0xff,
                            (off >>  8) & 0xff,
                             off        & 0xff] + list(chunk))
            self._wait_wip(dev)

    def _verify(self, dev, buf):
        got = self._xfer(dev, [0x03, 0, 0, 0], len(buf))
        if got == buf:
            return
        for i, (a, b) in enumerate(zip(buf, got)):
            if a != b:
                raise RuntimeError(
                    f"verify mismatch at 0x{i:06x}: "
                    f"wrote 0x{a:02x}, got 0x{b:02x}"
                )


class IcestickHandler(JobHandler):
    def __init__(self, serial_port=None, baudrate=115200, rx_duration_s=5):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.rx_duration_s = rx_duration_s
        self.icestick = Icestick()

    def _read_serial_for(self, ser, duration_s):
        start = time.time()
        buf = []
        while time.time() - start < duration_s:
            data = ser.read(1024)
            if data:
                buf.append(data)
        return b"".join(buf).decode(errors="replace")

    def run(self, payload, headers):
        ser = None
        try:
            if self.serial_port is not None:
                # DTR/RTS asserted on open send a glitch to the FPGA
                # side that corrupts the first received bytes, so clear
                # them explicitly.
                ser = serial.Serial(
                    self.serial_port,
                    baudrate=self.baudrate,
                    timeout=0.1,
                    dsrdtr=False, rtscts=False, xonxoff=False,
                )
                ser.setDTR(False)
                ser.setRTS(False)
                ser.reset_input_buffer()

            self.icestick.program(payload)

            duration = float(headers.get("X-Test-Runtime", self.rx_duration_s))
            if ser is not None:
                uart_msg = self._read_serial_for(ser, duration)
            else:
                uart_msg = f"programmed {len(payload)} bytes OK\n"
        finally:
            if ser is not None:
                ser.close()
        return {".txt": uart_msg}


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
        handler = self.handlers[kind]
        print(datetime.now(), kind, type(handler).__name__, job_id)

        artefacts = handler.run(payload, headers)

        # .txt is the completion sentinel -- every other artefact must be
        # on disk by the time submit.py sees it, so POST it last.
        txt = artefacts.pop(".txt", b"")
        for ext, data in artefacts.items():
            self.remote.post_resp(job_id, data, suffix=ext)
        self.remote.post_resp(job_id, txt)

    def run(self):
        try:
            while not self._stop:
                dispatched = False
                for kind in self.handlers:
                    payload, headers = self.remote.get_job(kind)
                    if payload:
                        self._dispatch(kind, payload, headers)
                        dispatched = True

                if not dispatched:
                    time.sleep(self.poll_interval_s)

        except KeyboardInterrupt:
            self._stop = True


def find_icestick_uart():
    """UART device of iCEstick FT2232H channel B.

    Windows: pyserial's COM-device hwid strings don't carry the MI_XX
    interface suffix, so we can't tell A from B from there. Ask D2XX
    for the COM number directly (FT_GetComPortNumber) -- it sees each
    interface as its own device.

    Linux: pyserial exposes the USB interface number in the
    location/hwid string as ``:1.N``. Channel B = interface 1, so
    match VID:PID 0403:6010 and the ``:1.1`` suffix.
    """
    if sys.platform == "win32":
        try:
            dev = ftd2xx.openEx(b"Dual RS232-HS B", 2)
        except ftd2xx.DeviceError:
            return None
        try:
            num = dev.getComPortNumber()
        finally:
            dev.close()
        return f"COM{num}" if num > 0 else None

    for p in serial.tools.list_ports.comports():
        if p.vid == 0x0403 and p.pid == 0x6010:
            if ":1.1" in (p.location or "") or ":1.1" in (p.hwid or ""):
                return p.device
    return None


def check_devices(ftdi_specs, com_specs, visa_specs):
    print(datetime.now(), "devices in use:")

    present = {d.decode(errors="replace")
               for d in (ftd2xx.listDevices(2) or [])}
    for desc, purpose in ftdi_specs:
        st = "ok" if desc in present else "MISSING"
        print(f"  [{st:>7}] FTDI {desc} -- {purpose}")

    present = {p.device for p in serial.tools.list_ports.comports()}
    for port, purpose in com_specs:
        if port is None:
            print(f"  [MISSING] COM  (not found) -- {purpose}")
        else:
            st = "ok" if port in present else "MISSING"
            print(f"  [{st:>7}] COM  {port} -- {purpose}")

    try:
        present = set(pyvisa.ResourceManager().list_resources())
    except Exception as e:
        print(f"  [MISSING] VISA -- backend unavailable: "
              f"{type(e).__name__}: {e}")
        present = set()
    for res, purpose in visa_specs:
        st = "ok" if res in present else "MISSING"
        print(f"  [{st:>7}] VISA {res} -- {purpose}")


def main():
    scope = "USB0::0xF4EC::0x1011::SDS2PDDX6R1848::INSTR"
    dsp_port = "COM31"
    ice_port = find_icestick_uart()
    check_devices(
        ftdi_specs=(
            ("FT4222 A", "DSP I/O expander + QSPI boot loader"),
            ("Dual RS232-HS A", "iCEstick SPI flash programmer"),
        ),
        com_specs=(
            (dsp_port, "DSP firmware UART output"),
            (ice_port, "iCEstick FPGA UART output"),
        ),
        visa_specs=(
            (scope, "oscilloscope trace capture"),
        ),
    )
    while True:
        try:
            handlers = {
                "ldr": DspHandler(
                    serial_port=dsp_port,
                    baudrate=115200,
                    rx_duration_s=5,
                    scope_resource=scope,
                ),
                "bin": IcestickHandler(serial_port=ice_port),
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
