# OctoPrint-PolarCloud (Beta)

Connects OctoPrint to the PolarCloud so that you can easily monitor and control
your printer from anywhere via https://polar3d.com

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
  portrait and choose Settings).
* Bring up the plugin's settings via OctoPrint-\>Settings-\>PolarCloud.
* Choose the closest printer type to your printer. If your printer isn't listed
  please choose "Cartesian" or "Delta" (as appropriate) and later in the Polar UI
  you can adjust things like print area and start/end gcode when setting up a print.
* Click the Register Printer button and fill out your email address and PIN
  number (for your Polar3D account).
* In a few moments it should fill out the Serial number field in OctoPrint
  settings, be sure to press "Save" on the Settings box to save the Serial Number.
* If you visit the Polar Cloud and click on the hamburger and choose
  "Printers", it should show your OctoPrint instance as one of your printers.

## Notes and Beta Limitations

* Printing from the cloud requires the CuraEngine path to be set properly in
  the Cura slicer plugin (octopi has this set up already).
* If you have a camera configured with OctoPrint you presently need to configure
  OctoPrint to use an absolute URL rather than a relative URL.  This so that the
  Polar Cloud's web interface can display the live camera feed in your web browser
  when you are on the same local network as your printer's camera.  The plugin
  will try to guess the absolute url if need be, but it can guess wrong if you
  have more than one network interface configured.
* While the Polar Cloud does present a "CONNECT" button and can therefore
  tell OctoPrint to connect via USB to your printer, the Polar Cloud cannot
  specify the serial device to use or the baud rate.  You will want to set
  and save those in OctoPrint.  Or, perhaps, avoid the issue entirely by
  configuring OctoPrint to automatically connect to your printer when OctoPrint
  starts.

## Note for Users running OctoPrint on Windows

If you've got an old version of pyopenssl installed into the OctoPrint
environment and you don't have ssh-keygen on your path, this plugin won't be
able to register your printer with Polar Cloud. You'll get something about
'ssh-keygen' not found in octoprint.log.  You can fix this by updating
pyOpenSSL and restarting OctoPrint.
