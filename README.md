# OctoPrint-PolarCloud

Connects OctoPrint to the PolarCloud so that you can easily monitor and control
your printer from anywhere via https://polar3d.com

## Setup

Install via OctoPrint's [Plugin Manager](https://github.com/foosel/OctoPrint/wiki/Plugin:-Plugin-Manager)
via OctoPrint-\>Settings-\>Plugin Manager-\>Get More... then search for Polar or
install via the "From URL" box using this URL:

    https://github.com/markwal/OctoPrint-PolarCloud/archive/master.zip

## Configuration

After installing the plugin and restarting OctoPrint, you need to register your
printer with your PolarCloud user account.
* Visit https://polar3d.com and setup a PIN in Account Settings (click on your
  portrait and choose Settings)
* Bring up the plugin's settings via OctoPrint-\>Settings-\>PolarCloud
* Choose the closest printer type to your printer. If your printer isn't listed
  please choose "Cartesion" or "Delta" (as appropriate) and later in the Polar UI
  you can adjust things like print area and start/end gcode when setting up a print.
* Click the Register Printer button and fill out your email address and PIN
  number (for your Polar3D account)
* In a few moments it should fill out the Serial number field in OctoPrint
  settings
* If you visit the Polar Cloud and click on the hamburger and choose
  "Printers", it should show your OctoPrint instance as one of your printers
