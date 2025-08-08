import os
import json
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from config import *

# Authenticate Drive and Sheets API
creds = Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)

# ========== Utilities ==========

def load_mappings():
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE, 'r') as f:
            return json.load(f)
    else:
        return {}

def save_mappings(mappings):
    with open(MAPPING_FILE, 'w') as f:
        json.dump(mappings, f, indent=4)

def escape_drive_query_string(s):
    """Escape single quotes for Google Drive API queries."""
    return s.replace("'", "\\'")

# ========== Drive Operations ==========

def find_folder(name, parent_id):
    """Return folder ID if exists, else None."""
    escaped_name = escape_drive_query_string(name)
    query = (
        f"mimeType='application/vnd.google-apps.folder' "
        f"and name='{escaped_name}' "
        f"and '{parent_id}' in parents "
        f"and trashed=false"
    )
    results = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def create_folder(name, parent_id):
    file_metadata = {
        'name': name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = drive_service.files().create(
        body=file_metadata,
        fields='id',
        supportsAllDrives=True
    ).execute()
    return folder.get('id')

def find_spreadsheet(name, parent_id):
    escaped_name = escape_drive_query_string(name)
    query = (
        f"mimeType='application/vnd.google-apps.spreadsheet' "
        f"and name='{escaped_name}' "
        f"and '{parent_id}' in parents "
        f"and trashed=false"
    )
    results = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def copy_spreadsheet_to_folder(device_name, freq, dest_folder_id):
    copied_title = f"{device_name} {freq[1]}"
    existing_id = find_spreadsheet(copied_title, dest_folder_id)
    if existing_id:
        print(f"  Spreadsheet '{copied_title}' already exists (ID: {existing_id})")
        return existing_id

    body = {
        'name': copied_title,
        'parents': [dest_folder_id]
    }

    copied_file = drive_service.files().copy(
        fileId=MASTER_SPREADSHEET_ID,
        body=body,
        fields='id',
        supportsAllDrives=True
    ).execute()
    print(f"  Created spreadsheet '{copied_title}' (ID: {copied_file['id']})")
    return copied_file.get('id')

def list_drive_files():
    results = drive_service.files().list(
        pageSize=100,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
        # Optionally add:
        # corpora='drive',
        # driveId='YOUR_SHARED_DRIVE_ID'
    ).execute()
    items = results.get('files', [])
    if not items:
        print('No files found.')
    else:
        for item in items:
            print(f"{item['name']} ({item['id']})")

# ========== Main Logic ==========

def setup_device_in_drive(device_name):
    """Ensure Google Drive folder + spreadsheets exist for a new device"""
    from config import FREQUENCIES, PROJECT_FOLDER_ID
    mappings = load_mappings()

    print(f"Setting up Google Drive for: {device_name}")

    # Ensure device folder exists
    folder_id = find_folder(device_name, PROJECT_FOLDER_ID)
    if not folder_id:
        folder_id = create_folder(device_name, PROJECT_FOLDER_ID)
        print(f"  Created folder '{device_name}' (ID: {folder_id})")
    else:
        print(f"  Folder '{device_name}' already exists (ID: {folder_id})")

    # Create/copy spreadsheet for each frequency
    for freq in FREQUENCIES:
        key = f"{device_name}_{freq[1]}"
        if key not in mappings:
            sheet_id = copy_spreadsheet_to_folder(device_name, freq, folder_id)
            mappings[key] = sheet_id
        else:
            print(f"  Mapping already exists: {key} → {mappings[key]}")

    save_mappings(mappings)
    print(f"  Drive setup complete for {device_name}")

