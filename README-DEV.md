# OctoPrint-PolorCloud Development Notes

## Install to run from source

Clone this repo onto your raspberry pi.  From the command line:

```
git clone https://github.com/markwal/OctoPrint-PolarCloud
```

Activate the octoprint python environment.  On my octopi, it's like this:

```
source ~/oprint/bin/activate
```

Then use pip to install this plugin in editable mode:

```
cd ~/OctoPrint-PolarCloud
pip install -e .
```

Restart octoprint to pick up the changes:

```
sudo service octoprint restart
```

## Making a new release

* Pull all changes to your local copy
* Restart the octoprint server
* Test
* Change the `plugin_version` number in setup.py
* Commit and push that change
* Make a new release in github (click "Releases", then "Draft new release"), 
  make sure that the version number matches the one you pushed.
