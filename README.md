MoniToni is the control daemon for a QR-code vending machine on a Raspberry Pi 5: one Python process owns the relays, LEDs, sensors and the touch UI served to a kiosk browser.
Run it on a laptop with mock hardware: `make dev`, then open http://127.0.0.1:8080/.
Installing on a real machine is described in [docs/SETUP.md](docs/SETUP.md).
