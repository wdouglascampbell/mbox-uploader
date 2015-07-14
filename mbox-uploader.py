'''
 '  Copyright 2015 Doug Campbell
 '
 '  This program is free software: you can redistribute it and/or modify
 '  it under the terms of the GNU General Public License as published by
 '  the Free Software Foundation, either version 3 of the License, or
 '  (at your option) any later version.
 '
 '  This program is distributed in the hope that it will be useful,
 '  but WITHOUT ANY WARRANTY; without even the implied warranty of
 '  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 '  GNU General Public License for more details.
 '
 '  You should have received a copy of the GNU General Public License
 '  along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import apiclient
import BaseHTTPServer
import email.utils
import getopt
import httplib2
import io
import logging
import mailbox
import os
import random
import simplejson
import sqlite3
import sys
import time
import webbrowser

from apiclient import errors
from credentials import *
from googleapiclient.discovery import build
from oauth2client.client import OAuth2WebServerFlow
from oauth2client.client import OAuth2Credentials
from oauth2client import GOOGLE_AUTH_URI
from oauth2client import GOOGLE_REVOKE_URI
from oauth2client import GOOGLE_TOKEN_URI
from urlparse import urlparse, parse_qs

# turn on logging
logging.basicConfig(filename='mbox-uploader.log',level=logging.INFO)

# configure needed Google Scopes
SCOPES = ("https://www.googleapis.com/auth/gmail.modify",)

# 32 backspaces
BS32 = "\b"*32


"""
 * parseCommandLine
 *
 * Parses the command line to see if either the --reauth or --redoallmessages
 * switches are used.
 *
 * Returns:
 *     True/False values for reauth and redoall
 *
"""
def parseCommandLine():
    # parse command line arguments
    # mbox-uploader.py [--reauth] [--redoallmessages]
    reauth = False
    redoall = False
    options, remainder = getopt.getopt(sys.argv[1:], "", ['reauth', 'redoallmessages'])
    for opt in options:
        if '--reauth' in opt:
            reauth = True
        if '--redoallmessages' in opt:
            redoall = True
    return reauth,redoall    

"""
 * getUserLabels
 *
 * Get a list all labels in account.
 *
 * Args:
 *     service: Authorized Gmail API service instance.
 *     user_id: Account email address. The special value "me"
 *              can be used to indicate the authenticated user.
 *
 * Returns:
 *     A list all labels in account.
"""
def getUserLabels(service, user_id):
    labels = {}
    
    try:
        response = service.users().labels().list(userId=user_id).execute()
        labelsList = response['labels']
        for label in labelsList:
            if label['type'] == "user":
                labels[label['name']] = label['id']
    except errors.HttpError, error:
        logging.error('function getUserLabels: An error occurred: %s' % error)
        print 'An error occurred: %s' % error
    
    return labels

"""
 * createLabel
 *
 * Create a new label for account.
 *
 * Args:
 *     service: Authorized Gmail API service instance.
 *     user_id: Account email address. The special value "me"
 *              can be used to indicate the authenticated user.
 *     label_object: label object for label to be added.
 *
 *  Returns:
 *     Label ID
"""
def createLabel(service, user_id, label_object):
    try:
        label = service.users().labels().create(userId=user_id,
                                                body=label_object).execute()
        return label['id']
    except errors.HttpError, error:
        logging.error('function createLabel: An error occurred: %s' % error)
        print 'An error occurred: %s' % error

"""
 * makeLabel
 *
 * Create label object.
 *
 * Args:
 *     label_name: The name of the Label.
 *     mlv: Message list visibility, show/hide.
 *     llv: Label list visibility, labelShow/labelHide.
 *
 * Returns:
 *    Created Label.
"""
def makeLabel(label_name, mlv='show', llv='labelShow'):
    label = {'messageListVisibility': mlv,
             'name': label_name,
             'labelListVisibility': llv}
    return label

"""
 * checkAddLabel
 *
 * If not exists, add label.
 *
 * Args:
 *     service: Authorized Gmail API service instance.
 *     label: label name
 *     current_labels: list of current labels for account
 *
"""
def checkAddLabel(service, label, current_labels):
    if label not in current_labels:
        current_labels[label] = createLabel(service,'me',makeLabel(label))
        
"""
 * getMigratedMessageIDs
 *
 * Retrieves a list of message-ID values for already migrated messages.
 *
 * Args:
 *     conn: database connection handler
 *     redoall: indicates whether to remove all message-id and migrate all messages again
 *
 * Returns:
 *     message-ID list
"""
def getMigratedMessageIDs(conn,redoall):
    c = conn.cursor()

    # does message_status table exist?
    message_status = []
    c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = ? AND name = ?", ["table", "message_status"])
    if c.fetchone()[0] > 0:
        # message_status table exists, retrieve message-id values
        if redoall:
            c.execute("DELETE FROM message_status")
            conn.commit()
        c.execute("SELECT * FROM message_status")
        while True:
            row = c.fetchone()
            if row is None:
                break
            message_id = row[0]
            message_status.append(message_id)
    else:
        # create table
        c.execute("CREATE TABLE message_status (message_id text unique)")
        conn.commit()

    return message_status

"""
 * getAuthCredentials
 *
 * Gets a credentials object that can be used to authorize a service object
 *
 * Args:
 *     conn: database connection handler
 *     reauth: indicates whether to remove all message-id and migrate all messages again
 *
"""
def getAuthCredentials(conn,reauth):
    c = conn.cursor()

    # does config table exist?
    c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = ? AND name = ?", ["table", "config"])
    if c.fetchone()[0] > 0:
        # config table exists, retrieve refresh_token if it exists
        c.execute("SELECT * FROM config WHERE name='refresh_token'")
        result = c.fetchone()
    else:
        result = None

    # retrieve refresh token, if one exists
    if result is None or reauth:
        # no refresh token.  need to get authorized.

        # get user authorization URL
        flow = OAuth2WebServerFlow(CLIENT_ID, CLIENT_SECRET, " ".join(SCOPES), redirect_uri="http://127.0.0.1:8000")
        auth_uri = flow.step1_get_authorize_url()

        print("\nLaunching your preferred web browser to continue sign-in at")
        print("your account provider website.  When you have granted access,")
        print("please return here to continue.")
        
        # open authorization url in preferred browser
        webbrowser.open(auth_uri)
        
        # start mini-webserver to listen for auth code response
        httpd = BaseHTTPServer.HTTPServer(('127.0.0.1', 8000), CustomHandler)
        httpd.handle_request()

        if auth_code:
            # retrieve authorization credentials using auth code
            credentials = flow.step2_exchange(auth_code)
            refresh_token = credentials.refresh_token
            print("\n\nAuthorization completed.\n")
        else:
            print("\nNo authorization code!")
            logging.info('Authorization Failed!')
            sys.exit("Exiting...")

        # create `config` table
        conn.execute("CREATE TABLE IF NOT EXISTS config (name text unique, value text)")

        # insert refresh_token into `config` table
        conn.execute("INSERT OR REPLACE INTO config (name, value) VALUES ('refresh_token','{0}')".format(refresh_token))

        # save changes
        conn.commit()
    else:
        refresh_token = result[1]
        credentials = OAuth2Credentials(None, CLIENT_ID,
                                   CLIENT_SECRET, refresh_token, None,
                                   GOOGLE_TOKEN_URI, None,
                                   revoke_uri=GOOGLE_REVOKE_URI,
                                   id_token=None,
                                   token_response=None)

    return credentials
    
"""
 * migrateMBOX
 *
 * Migrate MBOX.  Create label for MBOX folder, if necessary.  Migrate all
 * messages and set label.
 *
 * Args:
 *     service: Authorized Gmail API service instance.
 *     file: path to MBOX file
 *     label: label id to assign to MBOX messages
 *     message_status: list of all migrated message-ID
 *     conn: database connection handle
 *
"""
def migrateMBOX(service, file, label, message_status, conn):
    # open mbox file for reading
    mbox = mailbox.mbox(file)

    # get total number of messages in mbox file
    total_messages = len(mbox)
    
    # initialize labels
    # one label will always be 'CATEGORY_PERSONAL'
    # one label will be based on the label provided
    # one label will be 'UNREAD' if the message being uploaded has yet to be read
    labels = ['CATEGORY_PERSONAL',label]
    
    # iterate over all messages in mbox file
    msg_number = 0
    for message in mbox:
        msg_number += 1
        print(BS32+"Migrating message: {0} of {1}".format(str(msg_number).zfill(4),str(total_messages).zfill(4))),
        
        # has message already been uploaded
        message_id = message.__getitem__('message-id')
        if message_id in message_status:
            # skip it. it has already been uploaded
            # log some feedback
            logging.info("Message {0} of {1} - Already Uploaded - Skipped".format(msg_number,total_messages))
            continue
        
        # get x-mozilla-status value, if it exists
        x_mozilla_status = message.__getitem__('x-mozilla-status')
        
        if x_mozilla_status is not None:
            # determine if message is actually deleted
            if int(x_mozilla_status) & 8:
                # log some feedback
                logging.info("Message {0} of {1} - Already Deleted - Skipped".format(msg_number,total_messages))
                
                # skip message since it is marked as deleted
                continue
            
            # determine if message is unread
            if int(x_mozilla_status) == 0:
                labels.append("UNREAD")
        
        # remove x-mozilla-status and x-mozilla-status2 lines from message header
        message.__delitem__('x-mozilla-status')
        message.__delitem__('x-mozilla-status2')
        
        # extract raw message
        msg = message.as_string()
        
        # create file object to stream message contents
        fh = io.BytesIO(msg)
        
        # create media upload object
        media = apiclient.http.MediaIoBaseUpload( fh, mimetype='message/rfc822', chunksize=1024*1024, resumable=True )
        
        # import message
        postBody = { "labelIds": labels }
        
        # create import message request object
        request = service.users().messages().import_(userId='me', body=postBody, media_body=media, internalDateSource=None,
                                                     neverMarkSpam=True, processForCalendar=None, deleted=None) 

        # upload message in resumable chunks
        response = None
        upload_failed = False
        while response is None:
            try:
                status, response = request.next_chunk()
                '''
                if status:
                    sys.stdout.write('\r')
                    i = int(status.progress() * 20)
                    sys.stdout.write("[%-20s] %d%%" % ('='*i, 5*i))
                    sys.stdout.flush()
                '''
            except KeyboardInterrupt:
                print "\n\nUser ended execution"
                sys.exit()
            except:
                upload_failed = True
                logging.error("Message {0} of {1} - Upload Failed!".format(msg_number,total_messages))
                break
            
        if not upload_failed:
            conn.execute("INSERT into message_status VALUES ('{0}')".format(message_id))
            conn.commit()
            message_status.append(message_id)
            logging.info('Message {0} of {1} - "{2}"- Upload Complete'.format(msg_number,total_messages,message['subject']))

        # close StringIO object
        fh.close()
        
"""
 * CustomHandler class
 *
 * Modify base HTTP server to receive Google auth code and notify
 * user whether the authorization was successful.
 *
"""
class CustomHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        
        query_components = parse_qs(urlparse(self.path).query)
        if 'code' in query_components:
            auth_code = query_components['code'][0]
            response = "You may now close the browser tab and switch back to the application."
        else:
            auth_code = None
            response = "Authorization failed!"
            
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.send_header("Content-length", len(response))
        self.end_headers()
        self.wfile.write(response)
        
    # redefine log_message to prevent output to console log
    def log_message(self, format, *args):
        return
        
# parse command line arguments
reauth, redoall = parseCommandLine()

# open mbox-uploader database
try:
    conn = sqlite3.connect('mbox-uploader.db')
except sqlite3.Error:
    print "Error opening db.\n"

# get authorized credentials
credentials = getAuthCredentials(conn,reauth)

# Create an httplib2.Http object to handle our HTTP requests and authorize it
# with the credentials.
http = httplib2.Http()
http = credentials.authorize(http)

# get Gmail API service object
service = build('gmail', 'v1', http=http)

# get list of current labels
current_labels = getUserLabels(service, 'me')
current_labels["DRAFTS"] = "DRAFTS"
current_labels["INBOX"] = "INBOX"
current_labels["Incoming"] = "INBOX"
current_labels["SENT"] = "SENT"

# get list of message-ID for already migrated messages
message_status = getMigratedMessageIDs(conn,redoall)

# get %APPDATA% environment variable
APPDATA = os.environ['APPDATA']

# Set to Thunderbird Profiles directory
TBPROFILES = APPDATA + '\Thunderbird\Profiles'

# Get available profiles
profiles = os.listdir(TBPROFILES)

if len(profiles) > 1:
    # display profile selection menu
    print("Please select the number of the profile you wish to migrate:")
    
    # list available profiles
    selection = 0
    while not (selection > 0 and selection <= n):
        n = 0
        for profile in profiles:
            n += 1
            print("{0} : {1}".format(str(n).rjust(2),profile))
        try:
            selection = int(raw_input("Selection: "))
            if selection > 0 and selection <= n:
                selected_profile = profiles[selection-1]
            else:
                print("Invalid selection!  Please try again.")
        except:
            print("Invalid selection!  Please try again.")
else:
    selected_profile = profiles[0]

profile_dir = TBPROFILES + '\\' + selected_profile

# Get folders to migrate
print("Select which account folder to migrate or local folders, if available:")

folders = {}

# does ImapMail exist?
if os.path.exists(profile_dir + '\ImapMail'):
    # Yes:  are their directories in ImapMail folder?
    imapMailAccountFolders = os.listdir(profile_dir + '\ImapMail')
    for item in imapMailAccountFolders:
        if os.path.isdir(profile_dir + '\ImapMail\\' + item):
            folders[item] = profile_dir + '\ImapMail\\' + item
# does Mail exist?
if os.path.exists(profile_dir + '\Mail'):
    if os.path.exists(profile_dir + '\Mail\Local Folders'):
        folders['Local Folders'] = profile_dir + '\Mail\Local Folders'

# list account folders
selection = 0
while not (selection > 0 and selection <= n):
    n = 0
    folder_by_selection = []
    for label in folders:
        n += 1
        print("{0} : {1}".format(str(n).rjust(2),label))
        folder_by_selection.append(folders[label])
    try:
        selection = int(raw_input("Selection: "))
        if selection > 0 and selection <= n:
            selected_folder = folder_by_selection[selection-1]
        else:
            print("Invalid selection!  Please try again.")
    except:
        print("Invalid selection!  Please try again.")

mailroot = selected_folder

# Set to the root mail folder location
# For example, the local mail folder would be at:
#     APPDATA + '\Thunderbird\Profiles\iyvgb8d5.default\Mail\Local Folders'
#mailroot = APPDATA + '\Thunderbird\Profiles\iyvgb8d5.default\ImapMail\imap.googlemail.com'

print("\nBeginning migration...\n")

# walk mail folder structure and determine mail folder hierarchy and MBOX files to migrate
for dirName, subdirList, fileList in os.walk(mailroot):
    # Option 1 : Attempt to migrate Trash folder
    '''
    if os.path.basename(dirName) == '[Gmail].sbd':
        if 'Trash' in fileList:
            label = 'Trash'
            
            # output some feedback
            logging.info("Migrating folder: {0}".format(label))
            print("\rFolder: {0} ".format((label.ljust(55,' ')[:53] + '..') if len(label.ljust(55,' ')) > 55 else label.ljust(55,' ')))
            print(" *                                "),
            
            # migrate MBOX messages
            migrateMBOX(service, dirName + '\\Trash', current_labels[label], message_status, conn)
        continue
    '''
    for mboxFile in fileList:
        if mboxFile == "msgFilterRules.dat":
            # not an MBOX file
            continue
            
        fileName, fileExtension = os.path.splitext(mboxFile)
        if fileExtension == ".msf":
            # not an MBOX file
            continue
        
        if fileName in ['Unsent Messages','Trash']:
            # ignore messages that have not sent or in trash
            continue
            
        # build label name
        if dirName == mailroot:
            label = mboxFile
        else:
            label = dirName.replace(mailroot + '\\','').replace('.sbd','').replace('\\','/') + '/' + mboxFile
        
        # add label if it doesn't exist
        checkAddLabel(service, label, current_labels)
        
        # output some feedback
        logging.info("Migrating folder: {0}".format(label))
        print("\rFolder: {0} ".format((label.ljust(55,' ')[:53] + '..') if len(label.ljust(55,' ')) > 55 else label.ljust(55,' ')))
        print(" *                                "),
        
        # migrate MBOX messages
        migrateMBOX(service, dirName + '\\' + mboxFile, current_labels[label], message_status, conn)
        
    # Option 2 : Skip all Gmail custom folders
    if '[Gmail].sbd' in subdirList:
        del subdirList[subdirList.index("[Gmail].sbd")]
    
# close database connection
conn.close()

print("\r                                  ")
print("Migration Complete.\n")
raw_input("Press Enter to close application...")