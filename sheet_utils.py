import os
import json
from openpyxl.utils import get_column_letter
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from config import *

def _resolve_row(duty, voltage):
    """Return the row for the duty/voltage pair, falling back to the first row."""
    row_map = DUTY_ROW_MAP.get(duty)
    if row_map is None:
        raise KeyError(f"Duty cycle {duty} is not configured in DUTY_ROW_MAP")

    row = row_map.get(voltage)
    if row is not None:
        return row

    # Fall back to the first configured row (e.g. 200V) when voltage is unknown
    fallback_voltage, fallback_row = next(iter(row_map.items()))
    print(
        f"Voltage {voltage}V not found for duty {duty}%. "
        f"Defaulting to row mapped to {fallback_voltage}V (row {fallback_row})."
    )
    return fallback_row


def get_a1(sheet, config, duty, voltage, key):
    col = SHEET_MAPPINGS[config][key.lower()]
    row = _resolve_row(duty, voltage)
    return f"{sheet}!{get_column_letter(col)}{row}"

def load_mappings():
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE, 'r') as f:
            return json.load(f)
    else:
        return {}

def write_experiment_value(
    service,
    spreadsheet_id,
    sheet,
    config,
    duty,
    voltage,
    vin,
    iin,
    fsw,
    irms,
    vds_pk,
    isw,
):
    data = [
        {'range': get_a1(sheet, config, duty, voltage, "Vin"), 'values': [[vin]]},
        {'range': get_a1(sheet, config, duty, voltage, "Iin"), 'values': [[iin]]},
        {'range': get_a1(sheet, config, duty, voltage, "fsw"), 'values': [[fsw]]},
        {'range': get_a1(sheet, config, duty, voltage, "Irms"), 'values': [[irms]]},
        {'range': get_a1(sheet, config, duty, voltage, "Vds_pk"), 'values': [[vds_pk]]},
    ]
    if config == "Dual Conduction":
        data.append({'range': get_a1(sheet, config, duty, voltage, "Isw"), 'values': [[isw]]})

    body = {'valueInputOption': 'RAW', 'data': data}
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id, body=body).execute()
    print(
        f"Updated {config}, {duty*100:.0f}%, {voltage}V on {sheet} "
        f"with Vin={vin}, Iin={iin}, fsw={fsw}, Irms={irms}, Vds_pk={vds_pk}"
        + (f", Isw={isw}" if config == "Dual Conduction" else "")
    )


def clear_experiment_values(service, spreadsheet_id, sheet, config, duty, voltage):
    keys = ["Vin", "Iin", "fsw", "Irms", "Vds_pk"]
    ranges = [get_a1(sheet, config, duty, voltage, key) for key in keys]
    for range_name in ranges:
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            body={}
        ).execute()
    print(f"Cleared values for {config}, {duty:.0f}%, {voltage}V on {sheet}")
    
def clear_test_in_sheet(device_name, freq, temp, config, duty, voltage):
    mappings = load_mappings()
    frequency_label = f"{int(freq/1e6)}MHz"
    key = f"{device_name}_{frequency_label}"
    spreadsheet_id = mappings.get(key)
    if not spreadsheet_id:
        print(f"Spreadsheet for {key} not found in mapping.")
        return False

    sheet = f"{int(temp)}°C"
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('sheets', 'v4', credentials=creds)
    clear_experiment_values(service, spreadsheet_id, sheet, config, duty, voltage)
    return True

def log_final_results_to_sheet(
    device_name,
    freq,
    temp,
    config,
    duty,
    voltage,
    vin,
    iin,
    fsw,
    irms,
    vds_pk,
    isw,
):
    """Write final readings to the mapped Google Sheet and return status details."""
    from data_utils import freq_label

    frequency_label = freq_label(freq)
    key = f"{device_name}_{frequency_label}"

    try:
        mappings = load_mappings()
        spreadsheet_id = mappings.get(key)
        if not spreadsheet_id:
            error_message = (
                f"Spreadsheet for {key} not found in mapping file '{MAPPING_FILE}'."
            )
            print(error_message)
            return False, error_message

        sheet = f"{temp}°C"
        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        service = build('sheets', 'v4', credentials=creds)
        write_experiment_value(
            service,
            spreadsheet_id,
            sheet,
            config,
            duty,
            voltage,
            vin,
            iin,
            fsw,
            irms,
            vds_pk,
            isw,
        )
        return True, None
    except Exception as exc:
        error_message = (
            f"Google Sheets logging error for {key} at {temp}°C ({config}, {voltage}V, {duty} duty): {exc}"
        )
        print(error_message)
        return False, error_message
