#!/bin/sh
set -e

cat > /etc/udev/rules.d/99-bench-usb.rules <<'EOF'
# --- FTDI ---
# Detach ftdi_sio from FT2232H interface 0 (MPSSE) so libftd2xx can claim it.
# Interface 1 stays attached as /dev/ttyUSB* for the FPGA UART.
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6010", ATTR{bInterfaceNumber}=="00", RUN+="/bin/sh -c 'echo -n %k > /sys/bus/usb/drivers/ftdi_sio/unbind'"
# Allow non-root libusb access for all FTDI chips on the bench.
SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6010", MODE="0666"
SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="601c", MODE="0666"
SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6001", MODE="0666"

# --- STM32 DFU bootloader (used by STM32_Programmer_CLI) ---
SUBSYSTEM=="usb", ATTR{idVendor}=="0483", ATTR{idProduct}=="df11", MODE="0666"

# --- Siglent SDS2504X Plus scope (pyvisa-py via libusb) ---
SUBSYSTEM=="usb", ATTR{idVendor}=="f4ec", ATTR{idProduct}=="1011", MODE="0666"

# --- STM32MP135 baremetal USB MSC bootloader: /dev/sdX writable by user ---
SUBSYSTEM=="block", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="571d", MODE="0666"
EOF

udevadm control --reload-rules
udevadm trigger --action=change || true

echo "Bench USB rules updated. Replug new devices when ready."
