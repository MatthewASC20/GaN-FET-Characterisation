# data_utils.py
import os
import csv

def freq_label(freq_val):
    return f"{int(freq_val/1e6)}MHz"

def generate_filename(device_name, temp, freq, volt, config, duty, ensure_dirs=True):
    safe_device = "".join(c for c in device_name if c.isalnum() or c in (' ', '_', '-')).rstrip()
    base_folder = "Device Data"
    device_folder = os.path.join(base_folder, safe_device)
    freq_folder = os.path.join(device_folder, freq_label(freq))
    duty_folder = os.path.join(freq_folder, str(duty))
    if ensure_dirs:
        os.makedirs(duty_folder, exist_ok=True)
    filename = f"{safe_device}_{config}_{int(temp)}C_{freq_label(freq)}_{int(volt)}V_{duty}duty.csv"
    return os.path.join(duty_folder, filename)

def log_data_to_csv(filename, data, header=None):
    file_exists = os.path.isfile(filename)
    with open(filename, 'a', newline='') as csvfile:
        writer = csv.writer(csvfile)
        
        # Add final readings header if needed
        if data[0] == "FINAL_READINGS" and file_exists:
            writer.writerow([])  # Add empty row before final readings
            
        if not file_exists and header:
            writer.writerow(header)
            
        writer.writerow(data)

def load_tuned_frequency_from_csv(file_path: str) -> float | None:
    import csv
    if not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, newline='') as csvfile:
            reader = csv.reader(csvfile)
            for row in reader:
                if len(row) >= 5 and row[0].strip() == "FINAL_READINGS":
                    freq_val = row[4].strip()
                    return float(freq_val) if freq_val else None
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
    return None

def get_prior_tuned_frequency(self, device_name, freq, duty, volt, config):
    from data_utils import generate_filename

    # Try current config at 25°C
    primary_path = generate_filename(
        device_name, 25, freq, volt, config, duty, ensure_dirs=False
    )
    freq_val = load_tuned_frequency_from_csv(primary_path)
    if freq_val:
        return freq_val, primary_path

    # Fallback: Dual Conduction at 25°C
    fallback_path = generate_filename(
        device_name, 25, freq, volt, "Dual Conduction", duty, ensure_dirs=False
    )
    fallback_freq = load_tuned_frequency_from_csv(fallback_path)
    if fallback_freq:
        return fallback_freq, fallback_path

    return None, None
