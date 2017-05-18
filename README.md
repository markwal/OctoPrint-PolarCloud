# OctoPrint-PolarCloud

Connects OctoPrint to the PolarCloud so that you can easily monitor and control
your printer from anywhere via https://polar3d.com

## Setup

Install via OctoPrint's [Plugin Manager](https://github.com/foosel/OctoPrint/wiki/Plugin:-Plugin-Manager)
or manually using this URL:

    https://github.com/markwal/OctoPrint-PolarCloud/archive/master.zip

After installing and restarting OctoPrint, you need to register your printer with
your PolarCloud user account.
* In OctoPrint-\>Settings-\>PolarCloud, click the Register Printer button, this
  will register this OctoPrint instance with Polar3D and show a printer serial
  number which you'll need in a moment.
* Visit https://polar3d.com in a new tab
* Choose Printers from the hamburger menu in the upper left hand corner
* Click the "+ Add" button in the upper right
* It'll ask for your printer type and the serial number from earlier
* Then it'll ask you for the verification colors which should show up now in
  the OctoPrint tab (OctoPrint-\>Settings-\>PolarCloud-\>Register)

## Configuration

**TODO MarkWal:** talk about printer profiles here (PolarCloud profiles and
OctoPrint profiles)
