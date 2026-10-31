"""
Fridge controller functions from Zoe

IP is hard-coded for BFG

"""

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
from time import sleep


host = 'https://192.168.137.51'
port = '49098'
key = '98b3c002-4643-4bef-b209-870b221a2cef'


def getVarMap(var):
    return 'mapper.'+var.replace('/','.')

def getData(var):
    path = '/values/mapper/'+var
    dataRaw = requests.get(host+':'+port+path,params = {'key':key}, verify=False)
    data = dataRaw.json()
    return data

def getValue(var):
    data = getData(var)
    return data['data'][getVarMap(var)]['content']['latest_value']['value']


def getMXCHeaterPower():
    return float(getValue('temperature_control/heaters/sample/power'))*1e6

def getMXCTemperature():
    mxc_tempval = getValue('bf/temperatures/tmixing')
    if mxc_tempval == '':
        mxc_tempval = 0
    return float(mxc_tempval)*1e3

def getStillHeaterPower():
    return float(getValue('temperature_control/heaters/still/power'))*1e3

def getStillTemperature():
    return float(getValue('bf/temperatures/tstill'))*1e3

def get4KTemperature():
    return float(getValue('bf/temperatures/t4k'))

def get50KTemperature():
    return float(getValue('bf/temperatures/t50k'))

## pressure units mbar
def getP1Pressure():
    return float(getValue('bf/pressures/p1'))*1e3

def getP2Pressure():
    return float(getValue('bf/pressures/p2'))*1e3

def getP3Pressure():
    return float(getValue('bf/pressures/p3'))*1e3

def getP4Pressure():
    return float(getValue('bf/pressures/p4'))*1e3

def getP5Pressure():
    return float(getValue('bf/pressures/p5'))*1e3


## Additional variables for fridge monitor
def getFlow():
    return float(getValue('bf/flow'))

def getCompress1State():
    return bool(getValue('bflegacy/double/cparun'))

def getCompress2State():
    return bool(getValue('bflegacy/double/cparun_2'))

def getPulseTube():
    return bool(getValue('bf/pulsetube'))

def getCompress1WaterIn():
    return float(getValue('bflegacy/double/cpatempwi'))

def getCompress1WaterOut():
    return float(getValue('bflegacy/double/cpatempwo'))

def getCompress2WaterIn():
    return float(getValue('bflegacy/double/cpatempwi_2'))

def getCompress2WaterOut():
    return float(getValue('bflegacy/double/cpatempwo_2'))

def getAllVariables():
    path = '/values/mapper/'
    response = requests.get(host + ':' + port + path, params={'key': key}, verify=False)
    data = response.json()
    return list(data['data'].keys())
