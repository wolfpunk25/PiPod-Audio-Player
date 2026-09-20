import usb_cdc

# Keep the CircuitPython console and enable a separate data channel for the Pi.
usb_cdc.enable(console=True, data=True)
