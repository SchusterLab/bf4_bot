from BlueforsReader import *
from slack_sdk import WebClient
from time import sleep
import datetime


## Slack bot token
bot_token = 'xxxx'
client = WebClient(token=bot_token)


## Channel and canvas IDs
BFG_channel_ID = 'C0581FPR59U'
BFG_canvas_ID = 'F05FGLXUV1P'
hannah_channel_ID = 'U05MT0UJKA7' ## DM hannah


## Script config
sleep_time_min = 5       ## Time between checks
alarms_enable= False      ## Enable channel notifications
last_timestamp = None    ## Timestamp of last msg read in the channel
max_alarms = 20          ## Max number of channel notifications to avoid spam
alarm_count = 0          ## Current notification count


## Message channel if the abort temps are exceeded
abort_temps = {
    '50K'   : 80,
    '4K'    : 4,
    'STILL' : 1200,
    'MXC'   : 20,
    'Cryomech1 Input Water Temp' : 25,
    'Cryomech2 Input Water Temp' : 25,
    'Cryomech1 Output Water Temp' : 40,
    'Cryomech2 Output Water Temp' : 40,
}


## ----------------- Functions ----------------- ##
def get_temps():
    """Get the temperatures from the BFG"""
    return {
        '50K'   : get50KTemperature(),
        '4K'    : get4KTemperature(),
        'STILL' : getStillTemperature(),
        'MXC'   : getMXCTemperature(),
        "Cryomech1 Input Water Temp" : getCompress1WaterIn(),
        "Cryomech2 Input Water Temp" : getCompress2WaterIn(),
        "Cryomech1 Output Water Temp" : getCompress1WaterOut(),
        "Cryomech2 Output Water Temp" : getCompress2WaterOut()
    }

def get_pressures():
    """Get the pressures from the BFG"""
    return {
        "P1" : getP1Pressure(),
        "P2" : getP2Pressure(),
        "P3" : getP3Pressure(),
        "P4" : getP4Pressure(),
        "P5" : getP5Pressure(),
    }

def get_compressors():
    """Get the compressor info from the BFG"""
    return {
        "Flow" : getFlow(),
        'Pulse Tube' : getPulseTube(),
        "Cryomech1_Running" : getCompress1State(),
        "Cryomech2_Running" : getCompress2State(),
    }   

def message_channel(channel_id, message):
    """Send a message to a Slack channel"""
    response = client.chat_postMessage(
        channel=channel_id,
        text=message
    )
    return response

def check_for_mentions(channel_ID, last_timestamp=None, print_msg=True):
    """Check for mentions in a channel since last checked"""
    response = client.conversations_history(
        channel=channel_ID,
        oldest=last_timestamp
    )
    messages = response['messages']
    mentions = []
    if messages:
        last_timestamp = messages[0]['ts']
        for message in messages:
            if message['user'] == 'U08865JG9QT':
                print('Found my own message:', message['text'])
                continue
            if '<@U08865JG9QT>' in message['text']:
                mentions.append(message['text'])
                if print_msg: print(f"Found mention: {message['text']}")
    return last_timestamp, mentions


def format_status_message(alarm_str, temps, pressures, compress, print_msg=True):
    """Format the pressures and temps into a status message"""
    message = f"BFG Status -- channel notifications {alarm_str}\n\n"
    message += f"Last Updated - {datetime.datetime.now().strftime('%Y-%m-%d  %H.%M.%S')}\n\n"

    message += "**Temperatures:**\n\n"
    message += f"  50K: {temps['50K']} K\n\n"
    message += f"  4K: {temps['4K']} K\n\n"
    message += f"  Still: {temps['STILL']} mK\n\n"
    message += f"  MXC: {temps['MXC']} mK\n\n"

    message += '**Pressures:**\n\n'
    message += f"  P1: {pressures['P1']} mbar\n\n"
    message += f"  P2: {pressures['P2']} mbar\n\n"
    message += f"  P3: {pressures['P3']} mbar\n\n"
    message += f"  P4: {pressures['P4']} mbar\n\n"
    message += f"  P5: {pressures['P5']} mbar\n\n"
    message += f"  Flow: {compress['Flow']} mmol/s\n\n"


    message += '**Compressors:**\n\n'
    message += f"  Pulse Tube: {compress['Pulse Tube']} \n\n"
    message += f"  Cryomech1_Running: {compress['Cryomech1_Running']} \n\n"
    message += f"  Cryomech1 Input Water Temp: {temps['Cryomech1 Input Water Temp']} C\n\n"
    message += f"  Cryomech1 Output Water Temp: {temps['Cryomech1 Output Water Temp']} C\n\n"
    message += f"  Cryomech2_Running: {compress['Cryomech2_Running']} \n\n"
    message += f"  Cryomech2 Input Water Temp: {temps['Cryomech2 Input Water Temp']} C\n\n"
    message += f"  Cryomech2 Output Water Temp: {temps['Cryomech2 Output Water Temp']} C\n\n"

    if print_msg: print(message)
    return message


def update_canvas(canvas_ID, message):
    '''Update the canvas with the status message'''
    try: 
        response = client.canvases_edit(
            canvas_id = canvas_ID,
            changes = [{'operation': 'replace',
                    'document_content': {'type': 'markdown', 'markdown': message},
                    #'section_id' : section_id
                    }]
                )
    except Exception as e:
        print(f"Error updating canvas: {e}")
        return False
    return True


def compare_temps(temps, abort_temps, compress, channel_IDs):
    """Compare the temperatures to the abort temperatures and send an alarm if any are exceeded"""
    flag = False
    for key, value in temps.items():
        if value > abort_temps[key]:
            flag = True
            if key in ['50K', '4K']:
                unit = 'K'
            elif key in ['STILL', 'MXC']:
                unit ='mK'
            else: unit = 'C'
            description = 'plate' if key in key in ['STILL', 'MXC'] else ' '
            alarm_msg = f'@channel Warning! {key} {description} temperature is {value} {unit}'
            for channel_ID in channel_IDs: message_channel(channel_ID, alarm_msg)
            flag = True
            print(alarm_msg)
    ## HM 2/11/2025 adding compressor error message
    for key in ['Cryomech1_Running', 'Cryomech2_Running', 'Pulse Tube']:
        if compress[key] is False:
            alarm_msg = f'@channel Warning! {key} not running'
            for channel_ID in channel_IDs: message_channel(channel_ID, alarm_msg)
            flag = True
    return flag

def check_and_report(abort_temps, read_channel_ID, send_channel_IDs, canvas_ID, alarms_enable=True, last_timestamp=None):
    """Check the temperatures and report any alarms"""
    reset_alarm_count = False

    ## Check for mentions
    if last_timestamp is None:
        print('Zeroth loop so not sending notifications')
        last_timestamp, _ = check_for_mentions(read_channel_ID, print_msg=True)
    else:
        last_timestamp, mentions = check_for_mentions(read_channel_ID, last_timestamp)
        if len(mentions) > 0:
            for mention in mentions:
                print(mention)
                if any(keyword in mention for keyword in ['cancel', 'mute', 'disable', 'shut up', 'stop that nonsense', 'sshh', 'shush', 'quiet', 'zip it']):
                    alarms_enable = False
                    announcement = "okay I'll be quiet now :-(" if 'shut up' in mention else "Channel notifications have been disabled"
                elif any(keyword in mention for keyword in ['enable', 'reset', 'start', 'go', 'begin', 'speak', 'talk', 'get a job']):
                    alarms_enable = True
                    reset_alarm_count = True
                    announcement = "Channel notifications have been enabled"
                else:
                    announcement = f"Sorry, I don't understand '{mention}'!  Right now, I only respond to 'enable' and 'disable'"
                try:
                    response = client.chat_postMessage(
                        channel=read_channel_ID,
                        text=announcement
                    )
                except SlackApiError as e:
                    print(f"Error posting message: {e.response['error']}")              

    ## Collect temps from fridge        
    try:
        temps = get_temps()
    except Exception as e:
        print(f"Error getting temperatures: {e}")
        if alarms_enable:
            for channel_ID in send_channel_IDs: message_channel(channel_ID, f'Error getting temperatures: {e}')
    
    ## Collect pressures from fridge
    try:
        pressures = get_pressures()
    except Exception as e:
        print(f"Error getting pressures: {e}")
        if alarms_enable:
            for channel_ID in send_channel_IDs: message_channel(channel_ID, f'Error getting pressures: {e}')
    
    ## Collect compressor info from fridge
    try:
        compress = get_compressors()
    except Exception as e:
        print(f"Error getting compressor info: {e}")
        if alarms_enable:
            for channel_ID in send_channel_IDs: message_channel(channel_ID, f'Error getting pressures: {e}')

    if alarms_enable:
        warning_msg_sent = compare_temps(temps, abort_temps, compress, send_channel_IDs)
    else: warning_msg_sent = False

    ## Update the canvas
    status_post = format_status_message('enabled' if alarms_enable else 'disabled', temps, pressures, compress, print_msg=False)
    update_canvas(canvas_ID, status_post)
        
    return last_timestamp, warning_msg_sent, reset_alarm_count, alarms_enable


## ----------------- Main Loop ----------------- ##

if __name__ == '__main__':
    channel_IDs = [BFG_channel_ID]
    while True:
            last_timestamp, alarm_sent, reset_alarm_count, alarms_enable = check_and_report(abort_temps=abort_temps, 
                                  read_channel_ID = BFG_channel_ID, 
                                  send_channel_IDs = [BFG_channel_ID],
                                  canvas_ID = BFG_canvas_ID,
                                  alarms_enable=alarms_enable,
                                  last_timestamp=last_timestamp)
            if reset_alarm_count: alarm_count = 0
            elif alarm_sent: alarm_count += 1
            if alarm_count >= max_alarms: alarms_enable = False
            sleep(sleep_time_min*60)
            print('Alarm count:', alarm_count)
