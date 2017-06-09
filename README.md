# OctoPrint-PolarCloud

Connects OctoPrint to the PolarCloud so that you can easily monitor and control
your printer from anywhere via https://polar3d.com

## Setup

Install via OctoPrint's [Plugin Manager](https://github.com/foosel/OctoPrint/wiki/Plugin:-Plugin-Manager)
You can select it from the Plugin Repository via the Get More... button, or from
this URL:

    https://github.com/markwal/OctoPrint-PolarCloud/archive/master.zip

After installing and restarting OctoPrint, you need to register your printer with
your PolarCloud user account.
* Visit https://polar3d.com and setup a PIN in Account Settings (click on your
  portrait and choose Settings)
* In OctoPrint-\>Settings-\>PolarCloud, click the Register Printer button and
  fill out your email address and PIN number (for your Polar3D account)
* In a few moments it should fill out the Serial number field in OctoPrint
  settings
* If you visit the Polar Cloud and click on the hamburger and choose
  "Printers", it should show your OctoPrint instance as one of your printers

## Configuration

**TODO MarkWal:** talk about printer profiles here (PolarCloud profiles and
OctoPrint profiles)
