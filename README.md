To run:

```
python3 main.py -config <YOUR_YAML_CONFIG_KEY>
```

Please see the example [config.yaml](./config.yaml) file for the format of the config file.


Example:
```
python3 main.py -config ankara-general
```

You can define your own appointment options in the config.yaml by checking the example file. All you need to do is manually inspect the website once and get the options' text field you want to select.

Optional arguments:
```
--headless : Add if you want to run the script in headless mode.
--interval (int) : Interval between checks in seconds (default: 600 seconds)
--config_path (str) : Path to config file (default: config.yaml)
```

