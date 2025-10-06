# config.py
CONFIGURATIONS = [
    ("Dual Conduction", "Dual Conduction"),
    ("Single Conduction", "Single Conduction"),
    ("Single Device", "Single Device")
]
FREQUENCIES = [(6_000_000, "6MHz"), (13_000_000, "13MHz"), (27_000_000, "27MHz")]
DUTIES = [(25, "25%"), (50, "50%")]
TEMPERATURES = [(25, "25°C"), (40, "40°C"), (80, "80°C")]
VOLTAGES = [(200, "200V"), (300, "300V"), (400, "400V")]
DEFAULT_DURATION = 10
SERIAL_PORT = '/dev/tty.usbserial-PX95ZVF1'
BAUD_RATE = 9600
TIMEOUT = 1
GPIB_ADDR = 22


VOLTAGE_DEFAULT_MINUTES = {
    200: 1,
    300: 1,
    400: 1
}
# IP address of the instruments

INSTRUMENTS = {
    "SDM3055": {
        "ip": "10.2.90.56",
        "port": 5025
    },
    "SDG6022X": {
        "ip": "10.90.6.128",
        "port": 5025
    },
    "MSO44": {
        "ip": "10.90.7.130",
        "port": 4000
    }
}

# Below is the configuration for the google sheets/drive API

#Columns
SHEET_MAPPINGS = {
    "Dual Conduction": {
        "vin": 1,
        "iin": 2,
        "fsw": 5,
        "irms": 9,
        "vds_pk": 4,
        "isw": 12,
    },
    "Single Conduction": {
        "vin": 13,
        "iin": 14,
        "fsw": 17,
        "irms": 18,
        "vds_pk": 16,
    },
    "Single Device": {
        "vin": 22,
        "iin": 23,
        "fsw": 26,
        "irms": 27,
        "vds_pk": 25,
    },
}

DUTY_ROW_MAP = {
    25: {200: 4, 300: 5, 400: 6},
    50: {200: 11, 300: 12, 400: 13},
}

# Define scope
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]

SERVICE_ACCOUNT_FILE = 'credentials.json'   # Contains API Credentials (Expire at the end of UROP)
MAPPING_FILE = "spreadsheet_id_mappings.json" # Contains list of spreadsheet IDs

PROJECT_FOLDER_ID = '10deW0P-6I_JwlCQ66lxOE0cgC7wd_61S'  # <-- Set this to your Drive folder ID
MASTER_SPREADSHEET_ID = "1Fd3dU7KphvytGg7W_VU1HKoGyvk_ERY8OQiC0v3vwTM"  # <-- Set this to your master spreadsheet ID
