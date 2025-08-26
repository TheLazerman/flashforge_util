
import socket
import struct
import re
import time
import os
from socket_utils import *

def retrieve_file_list(socket):

    # Send the command to list files
    list_command = b'~M661\r\n'
    socket.sendall(list_command)

    # Function to receive a specific amount of data
    def recvall(sock, n):
        data = bytearray()
        while len(data) < n:
            packet = sock.recv(n - len(data))
            if not packet:
                return None
            data.extend(packet)
        return data

    # Read the initial textual reply
    while True:
        header_part = socket.recv(0x18)
        if b"ok\r\n" in header_part:
            break

    # Verify the list_start_signature
    list_start_signature = recvall(socket, 4)
    if not list_start_signature or struct.unpack('>I', list_start_signature)[0] != 0x44aaaa44:
        print("list_start_signature is incorrect. Exiting.")
        return

    # Read the file count
    file_count_data = recvall(socket, 4)
    if not file_count_data:
        print("Failed to read file count. Exiting.")
        return
    file_count = struct.unpack('>I', file_count_data)[0]



    file_list = []
    for _ in range(file_count):

        # There's a per-file signature: 3a 3a a3 a3
        list_start_signature = recvall(socket, 4)
        if not list_start_signature or struct.unpack('>I', list_start_signature)[0] != 0x3a3aa3a3:
            print("list_start_signature is incorrect. Exiting.")
            return

        # Read the length of the next file name
        name_length_data = recvall(socket, 4)
        if not name_length_data:
            print("Failed to read name length. Exiting.")
            return
        name_length = struct.unpack('>I', name_length_data)[0]

        # Read the file name
        file_name_data = recvall(socket, name_length)
        if not file_name_data:
            print("Failed to read file name. Exiting.")
            return
        file_name = file_name_data.decode('utf-8')
        file_list.append(file_name)

    return file_list

def find_file_on_printer(socket, search_filename):
    file_list = retrieve_file_list(socket)
    for filename in file_list:
        if search_filename in filename:
            return True, filename
    return False, None

def get_printer_info(socket):
    """ Returns an object with basic printer information such as name etc."""

    info_result = send_and_receive(socket, b'~M115\r\n')

    printer_info = {}
    printer_info_fields = ['Machine Type', 'Machine Name', 'Firmware', 'SN', 'X', 'Tool Count']
    for field in printer_info_fields:
        regex_string = field + ': ?(.+?)\\r\\n'
        match = re.search(regex_string, info_result.decode())

        if match:
            printer_info[field] = match.groups()[0]
        else:
            # Provide a default value if the field is not found
            printer_info[field] = None if field == 'CurrentFile' else None

    return printer_info


def get_print_progress(socket):
    info_result = send_and_receive(socket, b'~M27\r\n')

    regex_groups = re.search('([0-9].*)\/([0-9].*?)\\r', info_result.decode()).groups()
    printed = regex_groups[0]
    total = regex_groups[1]

    percentage = 0 if total == '0' else float((float(printed) / float(total)) * 100.0)

    return {'BytesPrinted': printed,
            'BytesTotal': total,
            'PercentageCompleted': percentage}

def get_printer_status(socket):
    info_result = send_and_receive(socket, b'~M119\r\n')

    printer_info = {}
    printer_info_fields = ['Status', 'MachineStatus', 'MoveMode', 'Endstop', 'LED', 'CurrentFile']
    for field in printer_info_fields:
        regex_string = field + ': ?(.+?)\\r\\n'
        match = re.search(regex_string, info_result.decode())

        if match:
            printer_info[field] = match.groups()[0]
        else:
            # Provide a default value if the field is not found
            printer_info[field] = None if field == 'CurrentFile' else None

    return printer_info

def parse_gx_file(filename):
    file = open(filename, mode='rb')
    file_contents = file.read()
    return len(file_contents), file_contents

def parse_gcode_file(filename):
    file = open(filename)
    lines = file.readlines()

    # The following is a hack to cleanup Cura-generated temperature commands that have a decimal point
    # in the temperature. Such commands trip up the FF firmware and causes the printer to ignore its temperature setting!
    print("Processing G-code to correct temperature settings...")

    # Define the regex pattern for matching temperature commands
    # This pattern looks for M104, M140, M190, or M109, followed by S and a number (with optional decimal part)
    pattern = re.compile(r'(M104 S|M140 S|M190 S|M109 S)(\d+)(\.\d+)?')

    # Process each line to fix temperature set commands
    processed_lines = []
    for line in lines:
        # Replace the matched pattern, removing the decimal part
        processed_line = pattern.sub(r'\1\2', line)
        processed_lines.append(processed_line)

    correction_count = 0
    for original, processed in zip(lines, processed_lines):
        if original != processed:
            correction_count += 1
            print ('Original:' + original)
            print('Processed:' + processed)

    print ('Corrected {} entries.'.format(correction_count))

    # Combine lines back into a single string and encode
    file_contents = ''.join(processed_lines).encode()
    return len(file_contents), file_contents

def upload_file(socket, filename):
    # First check filename to ensure it is below 36 characters (on my Guider 2s that causes the uploaded to silently fail):
    # Note that it LOOKS like they count the length including this prefix, so the actual limit is around 28 characters for the user's filename.
    length = len('0:/user/{}'.format(os.path.basename(filename)))

    if length > 42:
        print ("Filenames need to be 36 characters or less. Please shorten your filename and try again.")
        return False

    # Foward to appropriate file parser
    try:
        ext = os.path.splitext(filename)[1]
        if ext == ".gx":
            length, file_contents = parse_gx_file(filename)
        else:
            length, file_contents = parse_gcode_file(filename)
    except FileNotFoundError:
        print(f"Error: The file '{filename}' was not found.")
        return False
    except IOError:
        print(f"Error: An IO error occurred while opening the file '{filename}'.")
        return False

    encoded_command = '~M28 {} 0:/user/{}\r\n'.format(length, os.path.basename(filename)).encode()

    # Send the M28 command, preparing printer to receive file upload
    socket.sendall(encoded_command)
    # Receive confirmation for M28 command
    M28_response = socket.recv(BUFFER_SIZE)
    print(M28_response)

    # Upload the file
    send_data_with_progress(socket, file_contents)

    # Ensure separation of file data and EOF command
    time.sleep(.5)

    # Send M29 packet indicating EOF
    socket.sendall(b'~M29\r\n')
    M29_response = socket.recv(BUFFER_SIZE)
    print(M29_response)

    # TODO: Figure out how to actually determine file upload failures. This thing seems to just respond in the same way even when the file doesn't make it.
    return True

def print_file(socket, filename):
    response = send_and_receive(socket, '~M23 0:/user/{}\r\n'.format(filename).encode())

    # Regex pattern to find 'Size: ' followed by a number
    match = re.search(r'Size: (\d+)', response.decode('utf-8'))
    if match:
        # Extract the file size from the matched group
        file_size = int(match.group(1))

        if file_size > 0:
            return True

    return False

def resume_print(socket):
    info_result = send_and_receive(socket, '~M24'.encode())
    return info_result

def pause_print(socket):
    info_result = send_and_receive(socket, '~M25'.encode())
    return info_result

def cancel_print(socket):
    info_result = send_and_receive(socket, '~M26'.encode())
    return info_result

def unload_filament(socket):
    info_result = send_and_receive(socket, '~M702 U5\r\n'.encode())

    return info_result


def get_temperatures(socket):
    info_result = send_and_receive(socket, '~M105\r\n'.encode())

    pattern = re.compile(r'T0:(\d+) /(\d+) B:(\d+)/(\d+)')
    match = pattern.search(info_result.decode('utf-8'))
    if match:
        return {
            'Extruder_Current': match.group(1),
            'Extruder_Target': match.group(2),
            'Platform_Current': match.group(3),
            'Platform_Target': match.group(4)
        }

    return None

def control_obtain(socket):
    # This command "grabs" the printer
    original_timeout = socket.gettimeout()
    socket.settimeout(3)
    info_result = send_and_receive(socket, '~M601\r\n'.encode())
    socket.settimeout(original_timeout)
    if "Control Success" in info_result.decode():
        return True

    print ("Failed to acquired control of printer")
    return False

def control_release(socket):
    # This command "releases" the printer from the connection, allowing other FlashPrint to connect to it:
    info_result = send_and_receive(socket, '~M602\r\n'.encode())
    if "Control Release" in info_result.decode():
        print ("Successfully released control of printer")

    return
