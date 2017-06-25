# OctoPrint-PolarCloud (Beta)

Connects OctoPrint to the PolarCloud so that you can easily monitor and control
your printer from anywhere via https://polar3d.com

## Prereq

There's an incompatibility between the pip and pyOpenSSL that will often cause
problems with installing this plugin.  The following steps downgrade pip to a
version that can more reliably install pyOpenSSL.  Unfortunately, it'll also
give you that pip warning whenever you install a plugin (but is ignorable).

```
source ~/oprint/bin/activate
pip install --upgrade "pip>=8,<9"
pip install --upgrade pyOpenSSL
```

## Setup

Install via OctoPrint's [Plugin Manager](https://github.com/foosel/OctoPrint/wiki/Plugin:-Plugin-Manager)
via OctoPrint-\>Settings-\>Plugin Manager-\>Get More... then search for Polar or
install via the "From URL" box using this URL:

    https://github.com/markwal/OctoPrint-PolarCloud/archive/master.zip

## Enable Polar Cloud timelapses

To create timelapse movies in the format required by the Polar Cloud, the
plugin uses GStreamer.  To install GStreamer and the necessary plugins, use the
following command line:

```
sudo apt install gstreamer1.0-tools libx264-dev gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
```

## Plugin Configuration

After installing the plugin and restarting OctoPrint, you need to register your
printer with your PolarCloud user account.
* If you need a Polar Cloud account, you should start by creating one.  See the
  [project page](https://markwal.github.io/OctoPrint-PolarCloud) for a
  step-by-step instructions.
* Visit https://polar3d.com and setup a PIN in Account Settings (click on your
  portrait and choose Settings)
* Bring up the plugin's settings via OctoPrint-\>Settings-\>PolarCloud
* Choose the closest printer type to your printer. If your printer isn't listed
  please choose "Cartesian" or "Delta" (as appropriate) and later in the Polar UI
  you can adjust things like print area and start/end gcode when setting up a print.
* Click the Register Printer button and fill out your email address and PIN
  number (for your Polar3D account)
* In a few moments it should fill out the Serial number field in OctoPrint
  settings, be sure to press "Save" on the Settings box to save the Serial Number.
* If you visit the Polar Cloud and click on the hamburger and choose
  "Printers", it should show your OctoPrint instance as one of your printers

## Notes and Beta Limitations

* Currently the Polar cloud cannot cause OctoPrint to reconnect with your
  printer.  Therefore you'll want "Auto-connect on server startup" to be
  checked in OctoPrint's Serial Settings. You'll also want to make sure the
  correct port and baudrate are saved in settings.
* The plugin does not yet upload timelapse movies to OctoPrint
