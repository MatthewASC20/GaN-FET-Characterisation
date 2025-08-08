# experiment.py
import serial
import time
from data_utils import log_data_to_csv, generate_filename
from config import *

def initialize_connection(port, baud_rate, timeout):
    try:
        ser = serial.Serial(port, baud_rate, timeout=timeout)
        print(f"Connected to {port}")
        return ser
    except Exception as e:
        print(f"Error: {e}")
        return None

def configure_prologix(ser, gpib_addr):
    ser.write(b'++mode 1\n')
    ser.write(f'++addr {gpib_addr}\n'.encode())
    ser.write(b'++auto 1\n')
    ser.write(b'++eoi 1\n')
    ser.flush()
    time.sleep(0.5)

def clear_errors(ser):
    while True:
        ser.write(b'SYST:ERR?\n')
        time.sleep(0.1)
        response = ser.readline().decode().strip()
        if response == '+0,"No error"':
            break

def flush_buffer(ser):
    ser.reset_input_buffer()
    ser.reset_output_buffer()

def reset_to_local(ser):
    ser.write(b'++loc\n')
    time.sleep(0.5)

def read_complete_response(ser):
    response = ""
    while True:
        part = ser.readline().decode().strip()
        if not part:
            break
        response += part + "\n"
    return response.strip()

def measure_dc_current(ser):
    clear_errors(ser)
    flush_buffer(ser)
    ser.write(b'MEAS:CURR:DC?\n')
    time.sleep(0.7)
    response = read_complete_response(ser)
    flush_buffer(ser)
    try:
        return float(response)
    except ValueError:
        print(f"Invalid data received: {response}")
        return None
