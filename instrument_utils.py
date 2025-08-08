import socket
from config import INSTRUMENTS
import time
import datetime
import os
import pyvisa

WAVEGEN = "SDG6022X"
MULTIMETER = "SDM3055"
OSCILLOSCOPE = "MSO44"

def query_scpi(name, command, timeout=10):
    ip = INSTRUMENTS[name]["ip"]
    port = INSTRUMENTS[name]["port"]
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((command + "\n").encode())
            # Only try to receive if this is a query
            if command.strip().endswith("?"):
                return s.recv(1024).decode().strip()
            else:
                return None  # No response expected for set commands
    except socket.timeout:
        print(f"Timeout communicating with {name} for command: {command}")
        return None
    except Exception as e:
        print(f"Error communicating with {name}: {e}")
        return None

def configure_wavegen(config, freq, duty):
    """
    Configure SDG6022X channels for the selected mode using SCPI commands.
    Args:
        config (str): "Dual Conduction", "Single Conduction", or "Single Device"
        freq (float): Frequency in Hz
        duty (float or int): Duty cycle in percent (e.g., 25 or 50)
    """
    query_scpi("SDG6022X", "C2:OUTP OFF")
    query_scpi("SDG6022X", "C1:OUTP OFF")
    try:

        # Configure Channel 1: Square wave, 6.5Vpp, 1.9V offset, user freq/duty, phase 0
        cmd_c1 = f"C1:BSWV WVTP,SQUARE,FRQ,{freq},AMP,6.5VPP,OFST,1.9V,DUTY,{duty},PHSE,0"
        cmd_c2 = f"C2:BSWV WVTP,SQUARE,FRQ,{freq},AMP,6.5VPP,OFST,1.9V,DUTY,{duty},PHSE,0"
        query_scpi("SDG6022X", cmd_c1)

        time.sleep(0.2)

        if config == "Dual Conduction":
            # Copy all parameters from CH1 to CH2 (mirror)
            query_scpi("SDG6022X", cmd_c2)
            query_scpi("SDG6022X", "COUP STATE,ON")
            query_scpi("SDG6022X", "COUP FCOUP,ON")
            query_scpi("SDG6022X", "COUP DCOUP,ON")

        elif config == "Single Conduction":
            query_scpi("SDG6022X", "COUP STATE,OFF")
            # Set CH2 to DC waveform, 0V offset
            query_scpi("SDG6022X", "C2:BSWV WVTP,DC,OFST,0")
            time.sleep(0.2)


        elif config == "Single Device":
            # Turn off CH2 output
            pass


        else:
            raise ValueError(f"Unknown configuration: {config}")
        query_scpi("SDG6022X", "C1:OUTP OFF")
        # Final short delay for stability
        time.sleep(0.3)

    except Exception as e:
        print(f"Wavegen config error: {e}")
        raise
    print_channel_configurations()


def get_multimeter_voltage():

    res = query_scpi(MULTIMETER, "MEAS:VOLT:DC?")
    query_scpi(MULTIMETER, "SYST:LOC")
    return res

def get_wavegen_frequency():
    resp = query_scpi(WAVEGEN, "C1:BSWV?")
    for part in resp.split(','):
        if part.strip().endswith('HZ'):
            return part.split('HZ')[0].replace('HZ', '').strip()
    return resp

def get_oscilloscope_rms_current():
    return query_scpi(OSCILLOSCOPE, "MEASUrement:MEAS7:VALue?")


def capture_oscilloscope_screenshot_pyvisa(name, csv_file_path):
    ip = INSTRUMENTS[name]["ip"]
    # Use standard VISA address unless RAW SOCKET is required for your device
    visa_resource_addr = f'TCPIP0::{ip}::inst0::INSTR'
    folder_path = os.path.dirname(csv_file_path)
    base_name = os.path.basename(csv_file_path)
    base_name_no_ext = os.path.splitext(base_name)[0]
    screenshot_name = base_name_no_ext + ".png"
    screenshot_path = os.path.join(folder_path, screenshot_name)
    temp_on_scope = f'C:/{screenshot_name}'

    try:
        rm = pyvisa.ResourceManager()
        scope = rm.open_resource(visa_resource_addr)
        scope.timeout = 15000  # Set 15s timeout for long screenshot operations

        print("Connected to:", scope.query("*IDN?").strip())
        scope.write('SAVE:IMAGE:FILEFORMAT PNG')
        scope.write(f'SAVE:IMAGE "{temp_on_scope}"')
        scope.query("*OPC?")  # Wait for save to complete

        scope.write(f'FILESystem:READFile "{temp_on_scope}"')
        img_data = scope.read_raw(1024 * 1024)
        with open(screenshot_path, "wb") as f:
            f.write(img_data)
        print(f"Screenshot saved to: {screenshot_path}")
    except Exception as e:
        print(f"Oscilloscope screenshot failed: {e}")
    finally:
        try:
            scope.close()
            rm.close()
        except:
            pass

    return screenshot_path
    

def get_oscilloscope_peak_voltage():
    return query_scpi(OSCILLOSCOPE, "MEASUrement:MEAS6:VALue?")

def get_oscilloscope_isw_rms():
    return query_scpi(OSCILLOSCOPE, "MEASUrement:MEAS9:VALue?")

def parse_bswv_response(response):
    # Example response: 'C1:BSWV WVTP,SQUARE,FRQ,6000000,AMP,6.5VPP,OFST,1.9V,DUTY,50,PHSE,0'
    # Remove channel prefix and 'BSWV'
    if ':' in response:
        _, data_str = response.split(':', 1)
        data_str = data_str.replace('BSWV', '').strip()
    else:
        data_str = response.strip()
    tokens = data_str.split(',')
    config_dict = {}
    i = 0
    while i < len(tokens) - 1:
        key = tokens[i].strip()
        val = tokens[i+1].strip()
        config_dict[key] = val
        i += 2
    output = []
    output.append(f"Waveform Type: {config_dict.get('WVTP', 'Unknown')}")
    output.append(f"Frequency: {config_dict.get('FRQ', 'Unknown')} Hz")
    output.append(f"Amplitude: {config_dict.get('AMP', 'Unknown')}")
    output.append(f"Offset: {config_dict.get('OFST', 'Unknown')}")
    output.append(f"Duty Cycle: {config_dict.get('DUTY', 'Unknown')} %")
    output.append(f"Phase: {config_dict.get('PHSE', 'Unknown')} degrees")
    return '\n'.join(output)

def print_channel_configurations():
    # Query and print configuration for both channels
    for ch in ['C1', 'C2']:
        response = query_scpi("SDG6022X", f"{ch}:BSWV?")
        print(f"\n{ch} Configuration:")
        print(parse_bswv_response(response))


import numpy as np

import numpy as np

def get_cursor_values_at_times(channel, h):
    """
    Set waveform cursor A to 0 s and cursor B to h, and read their values on the given channel.
    Args:
        channel (int): Channel number (e.g., 1, 2, 3, 4)
        h (float): Half period in seconds

    Returns:
        (float, float): (Value at t=0, Value at t=h)
    """
    # Assign cursor source to the desired channel
    query_scpi(OSCILLOSCOPE, f"DISplay:WAVEView1:CURSor:CURSOR1:ASOUrce CH{channel}")
    query_scpi(OSCILLOSCOPE, f"DISplay:WAVEView1:CURSor:CURSOR1:BSOUrce CH{channel}")

    # Set cursor A to 0 s and cursor B to h
    query_scpi(OSCILLOSCOPE, "DISplay:WAVEView1:CURSor:CURSOR1:WAVEform:APOSition 0")
    query_scpi(OSCILLOSCOPE, f"DISplay:WAVEView1:CURSor:CURSOR1:WAVEform:BPOSition {h}")

    # Query amplitude at cursor A and B
    val_A = query_scpi(OSCILLOSCOPE, "DISplay:WAVEView1:CURSor:CURSOR1:WAVEform:AVPOSition?")
    val_B = query_scpi(OSCILLOSCOPE, "DISplay:WAVEView1:CURSor:CURSOR1:WAVEform:BVPOSition?")

    try:
        val_A = float(val_A)
    except Exception:
        print(f"Could not parse value at cursor A (t=0): {val_A}")
        val_A = None
    try:
        val_B = float(val_B)
    except Exception:
        print(f"Could not parse value at cursor B (t=h): {val_B}")
        val_B = None

    print(f"Channel {channel} value at t=0: {val_A}")
    print(f"Channel {channel} value at t=h ({h} s): {val_B}")

    return val_A, val_B

# Example usage:
#frequency = 4_600_000  # 6 MHz
#h = 1/(2*frequency)
#val_0, val_h = get_cursor_values_at_times(2, h)

