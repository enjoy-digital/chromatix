# Chromatic FPGA
This repository houses the ModRetro Chromatic's FPGA design files.

For more information about the ModRetro Chromatic, please see visit [ModRetro.com](https://modretro.com/).

## Setup

### Repository

This project builds upon the open source work provided by the Game Boy `MiSTer` project. When checking out this repository, make sure to run the following command as this repository submodules the Game Boy `MiSTer` project.

```bash
git submodule update --init --recursive
```

### Gowin Development Environment

**The Gowin FPGA Designer v1.9.9.03 must be used.** Using Gowin IDE v1.9.10.X or newer is currently not supported by this build.

You will also need to apply for a local license with Gowin through their website:
https://www.gowinsemi.com/en/support/license

The license expires after one year and will require reactivation.

You will receive an email within a few minutes with a `.lic` file attached. Run the Gowin IDE and install the license when it prompts you. You'll need to close and re-open the GOWIN IDE if everything was successful.

## Building
The active build flow is the LiteX top-level in `top.py`.

```bash
# Generate project files only.
python3 top.py --build --no-compile

# Full build with Gowin Designer in PATH.
python3 top.py --build
```

The generated bitstream is written to `build/chromatic.fs`.

## Flashing
Flashing can be performed using the official [Gowin Programmer](https://www.gowinsemi.com/en/) software or the [`openFPGALoader`](https://github.com/trabucayre/openFPGALoader) utility through the Chromatic's USB interface. The Gowin Programmer requires the installation of the GWU2X device driver.

Note:
1. The Chromatic must be powered on for either tool to detect the FPGA. This means the power switch is in the **ON** position.
2. If using `openFPGALoader`, the tool must be compiled with support for the Gowin GWU2X cable.

### Example Using `openFPGALoader`
**Detect the Chromatic FPGA While Powered On**
```bash
openFPGALoader --detect --cable gwu2x
```

You will see an output similar to:
```
empty
User requested: 6000000 real frequency is 6000000
index 0:
        idcode 0x1281b
        manufacturer Gowin
        family GW5A
        model  GW5A-25
        irlength 8
```

**Load the current LiteX bitstream temporarily**

```bash
openFPGALoader --cable gwu2x --bitstream build/chromatic.fs
```

**Program the current LiteX bitstream to flash**

```bash
openFPGALoader --write-flash --cable gwu2x --reset build/chromatic.fs
```

## Custom Modifications

When modifying the RTL design, please also update the 14-bit FPGA version within [esp32t/src/rtl/BSP/system_monitor.sv] around line 384 (see `version`).

This will ensure you can always using the [ModRetro Update Tool](https://modretro.com/pages/downloads#mrupdater) to restore your Chromatic to the latest official release.

## Issues
Please submit all issues and bug reports through our [Contact Form](https://modretro.com/pages/contact).

## Attributions
- [GOWIN Semiconductor](https://www.gowinsemi.com/en/)
- [MiSTer](https://github.com/MiSTer-devel/Gameboy_MiSTer)

## Special Thanks
- [rayjt9] For their palette improvements to the BootROM.
