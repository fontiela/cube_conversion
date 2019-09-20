import os
import subprocess
import glob
import re
import json
import argparse
import obspy
from obspy.geodetics import gps2dist_azimuth
import numpy as np
import matplotlib.pyplot as plt
import warnings

# -----------------------------------------------------------------------------
# Advanced configuration options
# -----------------------------------------------------------------------------
TRACE_DUR = 'HOUR'  # 'HOUR' is standard; other valid lengths can be used for
                    # the 'mseedcut' tool - see documentation

BITWEIGHT = 2.44140625e-7  # [V/ct]

DEFAULT_SENSITIVITY = 0.00902  # [V/Pa] Default sensor sensitivity
DEFAULT_OFFSET = -0.01529      # [V] Default digitizer offset

NUM_SATS = 9  # Minimum number of satellites required for keeping a GPS point

# Reverse polarity list for 2016 Yasur deployment
REVERSE_POLARITY_LIST = ['YIF1', 'YIF2', 'YIF3', 'YIF4', 'YIF5', 'YIF6',
                         'YIFA', 'YIFB', 'YIFC', 'YIFD']
# -----------------------------------------------------------------------------

# Set up command-line interface
parser = argparse.ArgumentParser(description='Convert DATA-CUBE files to '
                                             'miniSEED files while trimming, '
                                             'adding metadata, and renaming. '
                                             'Optionally extract coordinates '
                                             'from digitizer GPS.',
                                 allow_abbrev=False)
parser.add_argument('input_dir',
                    help='directory containing raw DATA-CUBE files (all files '
                         'must originate from a single digitizer)')
parser.add_argument('output_dir',
                    help='directory for output miniSEED and GPS-related files')
parser.add_argument('network',
                    help='desired SEED network code (2 characters, A-Z)')
parser.add_argument('station',
                    help='desired SEED station code (3-4 characters, A-Z & '
                         '0-9)')
parser.add_argument('location',
                    help='desired SEED location code (if AUTO, choose '
                         'automatically for 3 channel DATA-CUBE files)',
                    choices=['01', '02', '03', '04', 'AUTO'])
parser.add_argument('channel',
                    help='desired SEED channel code (if AUTO, determine '
                         'automatically using SEED convention [preferred])',
                    choices=['AUTO', 'BDF', 'HDF', 'DDF'])
parser.add_argument('-v', '--verbose', action='store_true',
                    help='enable verbosity for GIPPtools commands')
parser.add_argument('--grab-gps', action='store_true', dest='grab_gps',
                    help='additionally extract coordinates from digitizer GPS')
parser.add_argument('--bob-factor', default=None, type=float,
                    dest='breakout_box_factor',
                    help='factor by which to divide sensitivity values (for '
                         'custom breakout boxes)')
input_args = parser.parse_args()

# Check if input directory is valid
if not os.path.exists(input_args.input_dir):
    raise NotADirectoryError(f'Input directory \'{input_args.input_dir}\' '
                             'doesn\'t exist.')

# Check if output directory is valid
if not os.path.exists(input_args.output_dir):
    raise NotADirectoryError(f'Output directory \'{input_args.output_dir}\' '
                             'doesn\'t exist.')

# Check network code format
input_args.network = input_args.network.upper()
if not re.fullmatch('[A-Z]{2}', input_args.network):
    raise ValueError(f'Network code \'{input_args.network}\' is not valid.')

# Check station code format
input_args.station = input_args.station.upper()
if not re.fullmatch('[A-Z0-9]{3,4}', input_args.station):
    raise ValueError(f'Station code \'{input_args.station}\' is not valid.')

# Find directory containing this script
script_dir = os.path.dirname(__file__)

# Load digitizer-sensor pairings file
with open(os.path.join(script_dir, 'digitizer_sensor_pairs.json')) as f:
    digitizer_sensor_pairs = json.load(f)

# Load sensor sensitivities in V/Pa
with open(os.path.join(script_dir, 'sensor_sensitivities.json')) as f:
    sensitivities = json.load(f)

# Load digitizer offsets in V
with open(os.path.join(script_dir, 'digitizer_offsets.json')) as f:
    digitizer_offsets = json.load(f)

print('------------------------------------------------------------------')
print('Beginning conversion process...')
print('------------------------------------------------------------------')

# Print requested metadata
print(f' Network code: {input_args.network}')
print(f' Station code: {input_args.station}')
if input_args.location == 'AUTO':
    loc = 'Automatic'
else:
    loc = input_args.location
print(f'Location code: {loc}')
if input_args.channel == 'AUTO':
    cha = 'Automatic'
else:
    cha = input_args.channel
print(f' Channel code: {cha}')

# Gather info on files in the input dir (only search for files with extensions
# matching the codes included in `digitizer_sensor_pairs.json`)
raw_files = []
for digitizer_code in digitizer_sensor_pairs.keys():
    raw_files += glob.glob(os.path.join(input_args.input_dir,
                                        '*.' + digitizer_code))
raw_files.sort()  # Sort from earliest to latest in time
extensions = np.unique([f.split('.')[-1] for f in raw_files])
if extensions.size is 0:
    raise FileNotFoundError('No raw files found.')
elif extensions.size is not 1:
    raise ValueError(f'Files from multiple digitizers found: {extensions}')

# Create temporary processing directory in the output directory
tmp_dir = os.path.join(input_args.output_dir, 'tmp')
if not os.path.exists(tmp_dir):
    os.makedirs(tmp_dir)

# Get digitizer info and offset
digitizer = extensions[0]
try:
    offset = digitizer_offsets[digitizer]
except KeyError:
    warnings.warn('No matching offset values. Using default of '
                  f'{DEFAULT_OFFSET} V.')
    offset = DEFAULT_OFFSET
print(f'    Digitizer: {digitizer} (offset = {offset} V)')

# Get sensor info and sensitivity
sensor = digitizer_sensor_pairs[digitizer]
try:
    sensitivity = sensitivities[sensor]
except KeyError:
    warnings.warn('No matching sensitivities. Using default of '
                  f'{DEFAULT_SENSITIVITY} V/Pa.')
    sensitivity = DEFAULT_SENSITIVITY
print(f'       Sensor: {sensor} (sensitivity = {sensitivity} V/Pa)')

# Apply breakout box correction factor if provided
if input_args.breakout_box_factor:
    sensitivity = sensitivity / input_args.breakout_box_factor
    print('       Dividing sensitivity by breakout box factor of '
          f'{input_args.breakout_box_factor}')

print('------------------------------------------------------------------')
print(f'Running cube2mseed on {len(raw_files)} raw file(s)...')
print('------------------------------------------------------------------')

for raw_file in raw_files:
    print(os.path.basename(raw_file))
    args = ['cube2mseed', '--resample=SINC', f'--output-dir={tmp_dir}',
            '--encoding=FLOAT-64', raw_file]
    if input_args.verbose:
        args.append('--verbose')
    subprocess.call(args)

print('------------------------------------------------------------------')
print('Running mseedcut on converted miniSEED files...')
print('------------------------------------------------------------------')

# Create list of all day-long files
day_file_list = glob.glob(os.path.join(tmp_dir, '*'))

args = ['mseedcut', f'--output-dir={tmp_dir}', f'--file-length={TRACE_DUR}',
        tmp_dir]
if input_args.verbose:
    args.append('--verbose')
subprocess.call(args)

# Remove the day-long files from the temporary directory
for file in day_file_list:
    os.remove(file)

# Create list of all resulting cut files
cut_file_list = glob.glob(os.path.join(tmp_dir, '*'))
cut_file_list.sort()  # Sort from earliest to latest in time

print('------------------------------------------------------------------')
print(f'Adding metadata to {len(cut_file_list)} miniSEED file(s)...')
print('------------------------------------------------------------------')

# Loop through each cut file and assign the channel number, editing the simple
# metadata (automatically distinguish between a 3-element array or single
# sensor)
t_min, t_max = np.inf, -np.inf  # Initialize time bounds
for file in cut_file_list:
    print(os.path.basename(file))
    st = obspy.read(file)
    tr = st[0]
    tr.stats.network = input_args.network
    tr.stats.station = input_args.station

    if input_args.channel == 'AUTO':
        if 10 <= tr.stats.sampling_rate < 80:
            channel_id = 'BDF'
        elif 80 <= tr.stats.sampling_rate < 250:
            channel_id = 'HDF'
        elif 250 <= tr.stats.sampling_rate < 1000:
            channel_id = 'DDF'
        else:
            raise ValueError  # If the sampling rate is < 10 or >= 1000 Hz
    else:
        channel_id = input_args.channel

    tr.stats.channel = channel_id

    tr.data = tr.data * BITWEIGHT    # Convert from counts to V
    tr.data = tr.data + offset       # Remove voltage offset
    tr.data = tr.data / sensitivity  # Convert from V to Pa
    if input_args.station in REVERSE_POLARITY_LIST:
        tr.data = tr.data * -1

    if input_args.location == 'AUTO':
        if file.endswith('.pri0'):    # Channel 1
            location_id = '01'
            channel_pattern = '*.pri0'
        elif file.endswith('.pri1'):  # Channel 2
            location_id = '02'
            channel_pattern = '*.pri1'
        elif file.endswith('.pri2'):  # Channel 3
            location_id = '03'
            channel_pattern = '*.pri2'
        else:
            raise ValueError  # Should never reach this statement
    else:
        location_id = input_args.location
        channel_pattern = '*.pri?'  # Use all files

    tr.stats.location = location_id

    t_min = np.min([t_min, tr.stats.starttime])
    t_max = np.max([t_max, tr.stats.endtime])

    st.write(file, format='MSEED')

    # Define template for miniSEED renaming
    name_template = (f'{input_args.network}.{input_args.station}'
                     f'.{location_id}.{channel_id}.%Y.%j.%H')

    # Rename cut files and place in output directory
    args = ['mseedrename', f'--template={name_template}', '--force-overwrite',
            f'--include-pattern={channel_pattern}', '--transfer-mode=MOVE',
            f'--output-dir={input_args.output_dir}', file]
    if input_args.verbose:
        args.append('--verbose')
    subprocess.call(args)

# Extract digitizer GPS coordinates if requested
if input_args.grab_gps:

    print('------------------------------------------------------------------')
    print(f'Extracting/reducing GPS data for {len(raw_files)} raw file(s)...')
    print('------------------------------------------------------------------')

    # Create four-row container for data
    gps_data = np.empty((4, 0))

    # Define function to parse columns of input file
    def converter(string):
        return string.split('=')[-1]
    converters = {5: converter, 6: converter, 7: converter, 10: converter}

    # Loop over all raw files in input directory
    for raw_file in raw_files:
        gps_file = os.path.join(tmp_dir,
                                os.path.basename(raw_file)) + '.gps.txt'
        print(os.path.basename(gps_file))
        args = ['cubeinfo', '--format=GPS', f'--output-dir={tmp_dir}',
                raw_file]
        if input_args.verbose:
            args.append('--verbose')
        subprocess.call(args)

        # Read file and parse according to function above
        data = np.loadtxt(gps_file, comments=None, encoding='utf-8',
                          usecols=converters.keys(), converters=converters,
                          unpack=True)

        # Append the above data to existing array
        gps_data = np.hstack([gps_data, data])

        # Remove the file after reading
        os.remove(gps_file)

    # Remove lat/lon zeros from GPS errors
    gps_data = gps_data[:, (gps_data[0:2] != 0).all(axis=0)]

    # Threshold based on minimum number of satellites
    gps_data = gps_data[:, gps_data[3] >= NUM_SATS]
    if gps_data.size is 0:
        # Remove tmp directory (only if it's empty, to be safe!)
        if not os.listdir(tmp_dir):
            os.removedirs(tmp_dir)
        raise ValueError(f'No GPS points with at least {NUM_SATS} satellites '
                         'exist.')

    # Unpack to vectors
    (gps_lats, gps_lons, elev, sats) = gps_data

    # Merge coordinates
    output_coords = [np.median(gps_lats), np.median(gps_lons), np.median(elev)]

    # Write to JSON file - format is [lat, lon, elev] with elevation in meters
    json_filename = os.path.join(input_args.output_dir,
                                 f'{input_args.network}.{input_args.station}'
                                 f'.{input_args.location}.{channel_id}'
                                 '.json')
    with open(json_filename, 'w') as f:
        json.dump(output_coords, f)
        f.write('\n')

    print(f'Coordinates exported to {os.path.basename(json_filename)}')

    # Histogram prep
    INTERVAL = 0.00001
    x_edges = np.linspace(gps_lons.min() - INTERVAL / 2,
                          gps_lons.max() + INTERVAL / 2,
                          int(round((gps_lons.max() -
                                     gps_lons.min()) / INTERVAL)) + 2)
    y_edges = np.linspace(gps_lats.min() - INTERVAL / 2,
                          gps_lats.max() + INTERVAL / 2,
                          int(round((gps_lats.max() -
                                     gps_lats.min()) / INTERVAL)) + 2)

    # Create histogram
    hist = np.histogram2d(gps_lons, gps_lats,
                          bins=[x_edges.round(6), y_edges.round(6)])[0]
    hist[hist == 0] = np.nan
    hist = hist.T

    # Create x and y coordinate vectors
    xvec = np.linspace(gps_lons.min(), gps_lons.max(),
                       int(round((gps_lons.max() -
                                  gps_lons.min()) / INTERVAL)) + 1)
    yvec = np.linspace(gps_lats.min(), gps_lats.max(),
                       int(round((gps_lats.max() -
                                  gps_lats.min()) / INTERVAL)) + 1)
    xx, yy = np.meshgrid(xvec, yvec)

    # Convert to (lat, lon, counts) points
    counts = hist.ravel()
    lons = xx.ravel()
    lats = yy.ravel()

    # Convert from lat/lon to pseudoprojected x-y
    x, y = [], []
    for lat, lon in zip(lats, lons):
        dist, az, _ = gps2dist_azimuth(*output_coords[0:2], lat, lon)
        ang = np.deg2rad((450 - az) % 360)  # [Radians]
        x.append(dist * np.cos(ang))
        y.append(dist * np.sin(ang))

    # Convert to arrays
    x, y = np.array(x), np.array(y)

    # Make a figure
    fig, ax = plt.subplots(figsize=(10, 8))

    # Plot all GPS points
    sc = ax.scatter(x, y, c=counts, cmap='rainbow', zorder=3, clip_on=False)

    cbar = fig.colorbar(sc, label='Number of GPS points')

    # Plot median coordinate
    ax.scatter(0, 0, s=180, facecolor='none', edgecolor='black', zorder=3,
               clip_on=False,
               label=f'{tuple(output_coords[0:2])}\n'
                     f'{output_coords[2]} m elevation')

    ax.legend(title='Median coordinate:')

    # Aesthetic improvements
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_locator(plt.MultipleLocator(5))  # Ticks every 5 m
        axis.set_ticks_position('both')
    ax.minorticks_on()
    ax.set_aspect('equal')
    ax.grid(linestyle=':')

    ax.set_xlabel('Easting from median coordinate (m)')
    ax.set_ylabel('Northing from median coordinate (m)')

    fmt = '%Y-%m-%d %H:%M'
    ax.set_title(f'{gps_lons.size:,} GPS points with at least {NUM_SATS} '
                 f'satellites\n{t_min.strftime(fmt)} to {t_max.strftime(fmt)} '
                 'UTC', pad=20)

    png_filename = json_filename.rstrip('.json') + '.png'
    fig.savefig(png_filename, dpi=300, bbox_inches='tight')

    print('Coordinate overview figure exported to '
          f'{os.path.basename(png_filename)}')

# Remove tmp directory (only if it's empty, to be safe!)
if not os.listdir(tmp_dir):
    os.removedirs(tmp_dir)

print('------------------------------------------------------------------')
print('...finished conversion process.')
print('------------------------------------------------------------------')
