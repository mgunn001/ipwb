#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import sys
import os
import ipfsapi
import json
import subprocess
from subprocess import check_output
import pkg_resources
import surt
import re
import traceback
import signal
from pywb.utils.binsearch import iter_exact
from pywb.utils.canonicalize import unsurt
# from pywb.utils.canonicalize import canonicalize as surt
from flask import Flask
from flask import Response
from flask import request
from flask import redirect
from requests.exceptions import ConnectionError
from ipfsapi.exceptions import StatusError as hashNotInIPFS
from bisect import bisect_left
from socket import gaierror

import requests

import util as ipwbConfig
from util import IPFSAPI_IP, IPFSAPI_PORT, IPWBREPLAY_IP, IPWBREPLAY_PORT
from util import INDEX_FILE
from requests import ReadTimeout

from Crypto.Cipher import XOR
import base64

from werkzeug.routing import BaseConverter
from __init__ import __version__ as ipwbVersion

app = Flask(__name__)
app.debug = False

IPFS_API = ipfsapi.Client(IPFSAPI_IP, IPFSAPI_PORT)


@app.after_request
def setServerHeader(response):
    response.headers['Server'] = 'InterPlanetary Wayback Replay/' + ipwbVersion
    return response


@app.route('/webui/<path:path>')
def showWebUI(path):
    webuiPath = '/'.join(('webui', path)).replace('ipwb.replay', 'ipwb')
    content = pkg_resources.resource_string(__name__, webuiPath)

    if 'index.html' in path:
        iFile = ipwbConfig.getIPWBReplayIndexPath()

        if iFile is None or iFile == '':
            iFile = pkg_resources.resource_filename(__name__, INDEX_FILE)

        if not os.path.isabs(iFile):  # Convert rel to abs path
            iFileAbs = pkg_resources.resource_filename(__name__, iFile)
            if os.path.exists(iFileAbs):
                iFile = iFileAbs  # Local file

        content = content.replace(
            'MEMCOUNT', str(retrieveMemCount(iFile)))

        content = content.replace(
            'let uris = []',
            'let uris = {0}'.format(getURIsAndDatetimesInCDXJ(iFile)))
        content = content.replace('INDEXSRC', iFile)

    fileExtension = os.path.splitext(path)[1]

    mimeType = 'text/html'

    if fileExtension == '.js':
        mimeType = 'application/javascript'
    elif fileExtension == '.css':
        mimeType = 'text/css'

    resp = Response(content, mimetype=mimeType)
    resp.headers['Service-Worker-Allowed'] = '/'

    return resp


def getServiceWorker(path):
    path = ('/' + path).replace('ipwb.replay', 'ipwb')
    content = pkg_resources.resource_string(__name__, path)
    resp = Response(content, mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


class UnsupportedIPFSVersions(Exception):
    pass


@app.route('/daemon/<cmd>')
def commandDaemon(cmd):
    global IPFS_API
    if cmd == 'status':
        return generateDaemonStatusButton()
    elif cmd == 'start':
        subprocess.Popen(['ipfs', 'daemon'])
        # retString = 'Failed to start IPFS daemon'
        # if 'Daemon is ready' in check_output():
        #  retString = 'IPFS daemon started'
        return Response('IPFS daemon starting...')

    elif cmd == 'stop':
        try:
            installedIPFSVersion = IPFS_API.version()['Version']
            if ipwbConfig.compareVersions(installedIPFSVersion, '0.4.10') < 0:
                raise UnsupportedIPFSVersions()
            IPFS_API.shutdown()
        except (subprocess.CalledProcessError, UnsupportedIPFSVersions) as e:
            # go-ipfs < 0.4.10
            subprocess.call(['killall', 'ipfs'])
        return Response('IPFS daemon stopping...')
    else:
        print('ERROR, bad command sent to daemon API!')
        print(cmd)
        return Response('bad command!')


@app.route('/memento/*/<path:urir>')
def showMementosForURIRs(urir):
    if ipwbConfig.isLocalHosty(urir):
        urir = urir.split('/', 4)[4]
    s = surt.surt(urir, path_strip_trailing_slash_unless_empty=False)
    indexPath = ipwbConfig.getIPWBReplayIndexPath()

    print('Getting CDXJ Lines with the URI-R {0} from {1}'
          .format(urir, indexPath))
    cdxjLinesWithURIR = getCDXJLinesWithURIR(urir, indexPath)

    if len(cdxjLinesWithURIR) == 1:
        fields = cdxjLinesWithURIR[0].split(' ', 2)
        redirectURI = '/{1}/{0}'.format(unsurt(fields[0]), fields[1])
        return redirect(redirectURI, code=302)

    msg = ''
    if cdxjLinesWithURIR:
        msg += '<p>{0} capture(s) available:</p><ul>'.format(
            len(cdxjLinesWithURIR))
        for line in cdxjLinesWithURIR:
            fields = line.split(' ', 2)
            dt14 = fields[1]
            dtrfc1123 = ipwbConfig.datetimeToRFC1123(fields[1])
            msg += ('<li><a href="/{1}/{0}">{0} at {2}</a></li>'
                    .format(unsurt(fields[0]), dt14, dtrfc1123))
        msg += '</ul>'
    return Response(msg)


class RegexConverter(BaseConverter):
    def __init__(self, url_map, *items):
        super(RegexConverter, self).__init__(url_map)
        self.regex = items[0]
app.url_map.converters['regex'] = RegexConverter


@app.route('/memento/<regex("[0-9]{1,14}"):datetime>/<path:urir>')
def showMemento(urir, datetime):
    if ipwbConfig.isLocalHosty(urir):
        urir = urir.split('/', 4)[4]
    s = surt.surt(urir, path_strip_trailing_slash_unless_empty=False)
    indexPath = ipwbConfig.getIPWBReplayIndexPath()

    print('Getting CDXJ Lines with the URI-R {0} from {1}'
          .format(urir, indexPath))
    cdxjLinesWithURIR = getCDXJLinesWithURIR(urir, indexPath)

    closestLine = getCDXJLineClosestTo(datetime, cdxjLinesWithURIR)
    if closestLine is None:
        msg = '<h1>ERROR 404</h1>'
        msg += 'No capture found for {0} at {1}.'.format(urir, datetime)
        return Response(msg, status=404)

    uri = unsurt(closestLine.split(' ')[0])
    newDatetime = closestLine.split(' ')[1]
    return show_uri(uri, newDatetime)


def getCDXJLineClosestTo(datetimeTarget, cdxjLines):
    smallestDiff = float('inf')  # math.inf is only py3
    bestLine = None
    datetimeTarget = int(datetimeTarget)
    for cdxjLine in cdxjLines:
        dt = int(cdxjLine.split(' ')[1])
        diff = abs(dt - datetimeTarget)
        if diff < smallestDiff:
            smallestDiff = diff
            bestLine = cdxjLine
    return bestLine


def getCDXJLinesWithURIR(urir, indexPath):
    if not indexPath:
        indexPath = ipwbConfig.getIPWBReplayIndexPath()
    indexPath = getIndexFileFullPath(indexPath)

    print('Getting CDXJ Lines with {0} in {1}'.format(urir, indexPath))
    s = surt.surt(urir, path_strip_trailing_slash_unless_empty=False)
    cdxjLinesWithURIR = []

    cdxjLineIndex = getCDXJLine_binarySearch(s, indexPath, True, True)  # get i

    if cdxjLineIndex is None:
        return []

    cdxjLines = []
    with open(indexPath, 'r') as f:
        cdxjLines = f.read().split('\n')
        baseCDXJLine = cdxjLines[cdxjLineIndex]  # via binsearch

        cdxjLinesWithURIR.append(baseCDXJLine)

    # Get lines before pivot that match surt
    sI = cdxjLineIndex - 1
    while sI >= 0:
        if cdxjLines[sI].split(' ')[0] == s:
            cdxjLinesWithURIR.append(cdxjLines[sI])
        sI -= 1
    # Get lines after pivot that match surt
    sI = cdxjLineIndex + 1
    while sI < len(cdxjLines):
        if cdxjLines[sI].split(' ')[0] == s:
            cdxjLinesWithURIR.append(cdxjLines[sI])
        sI += 1
    return cdxjLinesWithURIR


@app.route('/timemap/<regex("link|cdxj"):format>/<path:urir>')
def showTimeMap(urir, format):
    s = surt.surt(urir, path_strip_trailing_slash_unless_empty=False)
    indexPath = ipwbConfig.getIPWBReplayIndexPath()

    cdxjLinesWithURIR = getCDXJLinesWithURIR(urir, indexPath)
    if format == "link":
        tm = generateLinkTimeMapFromCDXJLines(
            cdxjLinesWithURIR, s, request.url)
    elif format == "cdxj":
        tm = generateCDXJTimeMapFromCDXJLines(
            cdxjLinesWithURIR, s, request.url)

    return Response(tm)


def getLinkHeaderAbbreviatedTimeMap(urir, pivotDatetime):
    s = surt.surt(urir, path_strip_trailing_slash_unless_empty=False)
    indexPath = ipwbConfig.getIPWBReplayIndexPath()
    cdxjLinesWithURIR = getCDXJLinesWithURIR(urir, indexPath)
    hostAndPort = ipwbConfig.getIPWBReplayConfig()

    tmURI = 'http://{0}:{1}/timemap/link/{2}'.format(
        'localhost',  # hostAndPort[0],
        hostAndPort[1], urir)
    tm = generateLinkTimeMapFromCDXJLines(cdxjLinesWithURIR, s, tmURI)

    # Fix base TM relation when viewing abbrev version in Link resp
    tm = tm.replace('rel="self"', 'rel="timemap"')

    # Only one memento in TimeMap
    if 'rel="first last memento"' in tm:
        return tm

    tmLines = tm.split('\n')
    for idx, line in enumerate(tmLines):
        if len(re.findall('rel=.*memento"', line)) == 0:
            continue  # Not a memento

        if pivotDatetime in line:
            addBothNextAndPrev = False
            if idx > 0 and idx < len(tmLines) - 1:
                addBothNextAndPrev = True

            if addBothNextAndPrev or idx == 0:
                tmLines[idx + 1] = \
                    tmLines[idx + 1].replace('memento"', 'next memento"')
            if addBothNextAndPrev or idx == len(tmLines) - 1:
                tmLines[idx - 1] = \
                    tmLines[idx - 1].replace('memento"', 'prev memento"')
            break

    # Remove all mementos in abbrev TM that are not:
    #   first, last, prev, next, or pivot
    for idx, line in enumerate(tmLines):
        if len(re.findall('rel=.*memento"', line)) == 0:
            continue  # Not a memento
        if pivotDatetime in line:
            continue

        if len(re.findall('rel=.*(next|prev|first|last)', line)) == 0:
            tmLines[idx] = ''

    tm = '\n'.join(tmLines)

    return tm


def generateLinkTimeMapFromCDXJLines(cdxjLines, original, tmself):
    tmData = '<{0}>; rel="original",\n'.format(unsurt(original))
    tmData += '<{0}>; rel="self"; '.format(tmself)
    tmData += 'type="application/link-format",\n'
    hostAndPort = tmself[0:tmself.index('timemap/')]

    for i, line in enumerate(cdxjLines):
        (surtURI, datetime, json) = line.split(' ', 2)
        dtRFC1123 = ipwbConfig.datetimeToRFC1123(datetime)
        firstLastStr = ''

        if len(cdxjLines) > 1:
            if i == 0:
                firstLastStr = 'first '
            elif i == len(cdxjLines) - 1:
                firstLastStr = 'last '
        elif len(cdxjLines) == 1:
            firstLastStr = 'first last '

        tmData += '<{0}{1}/{2}>; rel="{3}memento"; datetime="{4}",\n'.format(
                hostAndPort, datetime, unsurt(surtURI),
                firstLastStr, dtRFC1123)
    tmData = tmData[0:-2]  # Trim final , and LF
    return tmData


def generateCDXJTimeMapFromCDXJLines(cdxjLines, original, tmself):
    tmData = '!context ["http://tools.ietf.org/html/rfc7089"]\n'
    tmData += '!id {{"uri": "{0}"}}\n'.format(tmself)
    tmData += '!keys ["memento_datetime_YYYYMMDDhhmmss"]\n'
    tmData += '!meta {{"original_uri": "{0}"}}\n'.format(unsurt(original))

    linkTMURI = tmself.replace('/timemap/cdxj/', '/timemap/link/')
    tmData += ('!meta {{"timemap_uri": {{'
               '"link_format": "{0}", '
               '"cdxj_format": "{1}"'
               '}}}}\n').format(linkTMURI, tmself)
    hostAndPort = tmself[0:tmself.index('timemap/')]

    for i, line in enumerate(cdxjLines):
        (surtURI, datetime, json) = line.split(' ', 2)
        dtRFC1123 = ipwbConfig.datetimeToRFC1123(datetime)
        firstLastStr = ''

        if len(cdxjLines) > 1:
            if i == 0:
                firstLastStr = 'first '
            elif i == len(cdxjLines) - 1:
                firstLastStr = 'last '
        elif len(cdxjLines) == 1:
            firstLastStr = 'first last '

        tmData += ('{1} {{'
                   '"uri": "{0}{1}/{2}", '
                   '"rel": "{3}memento", '
                   '"datetime"="{4}"}}\n').format(
                hostAndPort, datetime, unsurt(surtURI),
                firstLastStr, dtRFC1123)
    tmData = tmData[0:-1]  # Trim final , and LF
    return tmData


@app.route('/<regex("[0-9]{14}"):datetime>/<path:urir>')
def showMementoAtDatetime(urir, datetime):
    return show_uri(urir, datetime)


@app.errorhandler(Exception)
def all_exception_handler(error):
    print(error)
    return 'Error', 500


# This route needs better restructuring but is currently only used to get the
# webUI location for the ipwb webUI, more setting might need to be fetched in
# the future.
@app.route('/config/<requestedSetting>')
def getRequestedSetting(requestedSetting):
    return Response(ipwbConfig.getIPFSAPIHostAndPort() + '/webui')


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def show_uri(path, datetime=None):
    global IPFS_API

    if len(path) == 0:
        return showWebUI('index.html')
        sys.exit()

    if path == 'serviceWorker.js':
        return getServiceWorker(path)
        sys.exit()

    daemonAddress = '{0}:{1}'.format(IPFSAPI_IP, IPFSAPI_PORT)
    if not ipwbConfig.isDaemonAlive(daemonAddress):
        errStr = ('IPFS daemon not running. '
                  'Start it using $ ipfs daemon on the command-line '
                  ' or from the <a href="/">'
                  'IPWB replay homepage</a>.')
        return Response(errStr)
    cdxjLine = ''
    try:
        surtedURI = surt.surt(  # Good ol' pep8 line length
                     path, path_strip_trailing_slash_unless_empty=False)
        indexPath = ipwbConfig.getIPWBReplayIndexPath()

        searchString = surtedURI
        if datetime is not None:
            searchString = surtedURI + ' ' + datetime

        cdxjLine = getCDXJLine_binarySearch(searchString, indexPath)
        print('CDXJ Line: {0}'.format(cdxjLine))

    except:
        print(sys.exc_info()[0])
        respString = ('{0} not found :(' +
                      ' <a href="http://{1}:{2}">Go home</a>').format(
            path, IPWBREPLAY_IP, IPWBREPLAY_PORT)
        return Response(respString)
    if cdxjLine is None:  # Resource not found in archives
        msg = '<h1>ERROR 404</h1>'
        msg += 'No capture found for {0} at {1}.'.format(path, datetime)
        linesWithSameURIR = getCDXJLinesWithURIR(path, None)
        print('CDXJ lines with URI-R at {0}'.format(path))
        print(linesWithSameURIR)

        # TODO: Use closest instead of conditioning on single entry
        #  temporary fix for core functionality in #225
        if len(linesWithSameURIR) == 1:
            fields = linesWithSameURIR[0].split(' ', 2)
            redirectURI = '/{1}/{0}'.format(unsurt(fields[0]), fields[1])
            return redirect(redirectURI, code=302)

        if linesWithSameURIR:
            msg += '<p>{0} capture(s) available:</p><ul>'.format(
                  len(linesWithSameURIR))
            for line in linesWithSameURIR:
                fields = line.split(' ', 2)
                msg += ('<li><a href="/{1}/{0}">{0} at {1}</a></li>'
                        .format(unsurt(fields[0]), fields[1]))
            msg += '</ul>'
        return Response(msg, status=404)

    cdxjParts = cdxjLine.split(" ", 2)
    jObj = json.loads(cdxjParts[2])
    datetime = cdxjParts[1]

    digests = jObj['locator'].split('/')

    class HashNotFoundError(Exception):
        pass

    try:
        def handler(signum, frame):
            raise HashNotFoundError()

        signal.signal(signal.SIGALRM, handler)
        signal.alarm(10)

        payload = IPFS_API.cat(digests[-1])
        header = IPFS_API.cat(digests[-2])

        signal.alarm(0)

    except ipfsapi.exceptions.TimeoutError:
        print("{0} not found at {1}".format(cdxjParts[0], digests[-1]))
        respString = ('{0} not found in IPFS :(' +
                      ' <a href="http://{1}:{2}">Go home</a>').format(
            path, IPWBREPLAY_IP, IPWBREPLAY_PORT)
        return Response(respString)
    except TypeError:
        print('A type error occurred')
        print(traceback.format_exc())
        print(sys.exec_info()[0])
    except HashNotFoundError:
        print("Hashes not found")
        return '', 404
    except:
        print('Unknown exception occurred while fetching from ipfs.')
        sys.exit()

    if 'encryption_method' in jObj:
        keyString = None
        while keyString is None:
            if 'encryption_key' in jObj:
                keyString = jObj['encryption_key']
            else:
                askForKey = ('Enter a path for file',
                             ' containing decryption key: \n> ')
                keyString = raw_input(askForKey)

        encryptionMethod = None
        if jObj['encryption_method'] == 'xor':
            encryptionMethod = XOR

        pKey = encryptionMethod.new(keyString)
        payload = pKey.decrypt(base64.b64decode(payload))
        hKey = encryptionMethod.new(keyString)
        header = hKey.decrypt(base64.b64decode(header))

    hLines = header.split('\n')
    hLines.pop(0)

    resp = Response(payload)

    for idx, hLine in enumerate(hLines):
        k, v = hLine.split(': ', 1)

        if k.lower() == 'transfer-encoding' and v.lower() == 'chunked':
            try:
                unchunkedPayload = extractResponseFromChunkedData(payload)
            except:
                continue  # Data may have no actually been chunked
            resp.set_data(unchunkedPayload)

        if k.lower() != "content-type":
            k = "X-Archive-Orig-" + k

        resp.headers[k] = v

    # Add ipwb header for additional SW logic
    newPayload = resp.get_data()
    ipwbjsinject = """<script src="/webui/webui.js"></script>
                      <script>injectIPWBJS()</script>"""
    newPayload = newPayload.replace('</html>', ipwbjsinject + '</html>')
    resp.set_data(newPayload)

    resp.headers['Memento-Datetime'] = ipwbConfig.datetimeToRFC1123(datetime)

    # Get TimeMap for Link response header
    respWithLinkHeader = getLinkHeaderAbbreviatedTimeMap(path, datetime)
    resp.headers['Link'] = respWithLinkHeader.replace('\n', ' ')

    return resp


def extractResponseFromChunkedData(data):
    chunkDescriptor = -1
    retStr = ''

    (chunkDescriptor, rest) = data.split('\n', 1)
    chunkDescriptor = chunkDescriptor.split(';')[0].strip()

    while chunkDescriptor != '0':
        # On fail, exception, delta in header vs. payload chunkedness
        chunkDecFromHex = int(chunkDescriptor, 16)  # Get dec for slice
        retStr += rest[:chunkDecFromHex]  # Add to payload
        rest = rest[chunkDecFromHex:]  # Trim from the next chunk onward
        (CRLF, chunkDescriptor, rest) = rest.split('\n', 2)
        chunkDescriptor = chunkDescriptor.split(';')[0].strip()

        if len(chunkDescriptor.strip()) == 0:
            break
    return retStr


def generateDaemonStatusButton():
    text = 'Not Running'
    buttonText = 'Start'
    if ipwbConfig.isDaemonAlive():
        text = 'Running'
        buttonText = 'Stop'

    statusPageHTML = '<html id="status{0}" class="status">'.format(buttonText)
    statusPageHTML += ('<head><base href="/webui/" /><link rel="stylesheet" '
                       'type="text/css" href="webui.css" />'
                       '<script src="webui.js"></script>'
                       '<script src="daemonController.js"></script>'
                       '</head><body>')
    buttonHTML = '<span id="status">{0}</span>'.format(text)
    buttonHTML += '<button id="daeAction">{0}</button>'.format(buttonText)

    footer = '<script>assignStatusButtonHandlers()</script></body></html>'
    return Response('{0}{1}{2}'.format(statusPageHTML, buttonHTML, footer))


def fetchRemoteCDXJFile(path):
    fileContents = ''
    path = path.replace('ipfs://', '')
    # TODO: Take into account /ipfs/(hash), first check if this is correct fmt

    if '://' not in path:  # isAIPFSHash
        # TODO: Check if a valid IPFS hash
        print('No scheme in path, assuming IPFS hash and fetching...')
        try:
            print("Trying to ipfs.cat('{0}')".format(path))
            dataFromIPFS = IPFS_API.cat(path)
        except hashNotInIPFS:
            return ''
        except:
            print("An error occurred with ipfs.cat")
            print(sys.exc_info()[0])
            sys.exit()
        print('Data successfully obtained from IPFS')
        return dataFromIPFS
    else:  # http://, ftp://, smb://, file://
        print('Path contains a scheme, fetching remote file...')
        fileContents = ipwbConfig.fetchRemoteFile(path)
        return fileContents

    # TODO: Check if valid CDXJ here before returning
    return fileContents


def getIndexFileContents(cdxjFilePath=INDEX_FILE):
    if not os.path.exists(cdxjFilePath):
        print('File {0} does not exist locally, fetching remote'.format(
                                                                 cdxjFilePath))
        return fetchRemoteCDXJFile(cdxjFilePath) or ''

    indexFilePath = '/{0}'.format(cdxjFilePath).replace('ipwb.replay', 'ipwb')
    print('getting index file at {0}'.format(indexFilePath))

    indexFileContent = ''
    with open(cdxjFilePath, 'r') as f:
        indexFileContent = f.read()

    return indexFileContent


def getIndexFileFullPath(cdxjFilePath=INDEX_FILE):
    indexFilePath = '/{0}'.format(cdxjFilePath).replace('ipwb.replay', 'ipwb')

    if os.path.isfile(cdxjFilePath):
        return cdxjFilePath

    indexFileName = pkg_resources.resource_filename(__name__, indexFilePath)
    return indexFileName


def getURIsAndDatetimesInCDXJ(cdxjFilePath=INDEX_FILE):
    indexFileContents = getIndexFileContents(cdxjFilePath)

    if not indexFileContents:
        return 0

    lines = indexFileContents.strip().split('\n')

    uris = {}
    for i, l in enumerate(lines):
        if not ipwbConfig.isValidCDXJLine(l):
            continue

        if ipwbConfig.isCDXJMetadataRecord(l):
            continue

        cdxjFields = l.split(' ', 2)
        uri = unsurt(cdxjFields[0])
        datetime = cdxjFields[1]

        try:
            jsonFields = json.loads(cdxjFields[2])
        except:  # Skip lines w/o JSON block
            continue

        if uri not in uris:
            uris[uri] = {}
            uris[uri]['datetimes'] = []
        uris[uri]['datetimes'].append(datetime)
        uris[uri]['mime'] = jsonFields['mime_type']

        pass
    return json.dumps(uris)


def retrieveMemCount(cdxjFilePath=INDEX_FILE):
    print("Retrieving URI-Ms from {0}".format(cdxjFilePath))
    indexFileContents = getIndexFileContents(cdxjFilePath)

    if not indexFileContents:
        return 0
    lines = indexFileContents.strip().split('\n')

    if not lines:
        return 0
    mementoCount = 0

    for i, l in enumerate(lines):
        validCDXJLine = ipwbConfig.isValidCDXJLine(l)
        metadataRecord = ipwbConfig.isCDXJMetadataRecord(l)
        if validCDXJLine and not metadataRecord:
            mementoCount += 1

    return mementoCount


def objectifyCDXJData(lines, onlyURI):
    cdxjData = {'metadata': [], 'data': []}
    for line in lines:
        if len(line.strip()) == 0:
            break
        if line[0] != '!':
            (surt, datetime, theRest) = line.split(' ', 2)
            searchString = "{0} {1}".format(surt, datetime)
            if onlyURI:
                searchString = surt
            cdxjData['data'].append(searchString)
        else:
            cdxjData['metadata'].append(line)
    return cdxjData


def binary_search(haystack, needle, returnIndex=False, onlyURI=False):
    lBound = 0
    uBound = None

    surtURIsAndDatetimes = []

    cdxjObj = objectifyCDXJData(haystack, onlyURI)
    surtURIsAndDatetimes = cdxjObj['data']

    metaLineCount = len(cdxjObj['metadata'])

    if uBound is not None:
        uBound = uBound
    else:
        uBound = len(surtURIsAndDatetimes)

    pos = bisect_left(surtURIsAndDatetimes, needle, lBound, uBound)

    if pos != uBound and surtURIsAndDatetimes[pos] == needle:
        if returnIndex:  # Index useful for adjacent line searching
            return pos + metaLineCount
        return haystack[pos + metaLineCount]
    else:
        return None


def getCDXJLine_binarySearch(
         surtURI, cdxjFilePath=INDEX_FILE, retIndex=False, onlyURI=False):
    fullFilePath = getIndexFileFullPath(cdxjFilePath)

    with open(fullFilePath, 'r') as cdxjFile:
        lines = cdxjFile.read().split('\n')

        lineFound = binary_search(lines, surtURI, retIndex, onlyURI)
        if lineFound is None:
            print("Could not {0} in CDXJ at {1}".format(surtURI, fullFilePath))

        return lineFound


def start(cdxjFilePath=INDEX_FILE):
    hostPort = ipwbConfig.getIPWBReplayConfig()
    if not hostPort:
        ipwbConfig.setIPWBReplayConfig(IPWBREPLAY_IP, IPWBREPLAY_PORT)
        hostPort = ipwbConfig.getIPWBReplayConfig()

    if ipwbConfig.isDaemonAlive():
        ipwbConfig.firstRun()
        ipwbConfig.setIPWBReplayIndexPath(cdxjFilePath)
        app.cdxjFilePath = cdxjFilePath
    else:
        print('Sample data not pulled from IPFS.')
        print('Check that the IPFS daemon is running.')

    try:
        app.run(host=IPWBREPLAY_IP, port=IPWBREPLAY_PORT)
    except gaierror:
        print('Detected no active Internet connection.')
        print('Overriding to use default IP and port configuration.')
        app.run()


if __name__ == "__main__":
    start()

# Read in URI, convert to SURT
#  surt(uriIn)
# Get SURTed URI lines in CDXJ
#  Read CDXJ
#  Do bin search to find relevant lines

# read IPFS hash from relevant lines (header, payload)

# Fetch IPFS data at hashes
