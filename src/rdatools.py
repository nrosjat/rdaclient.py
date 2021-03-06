'''
Some useful tools to work with BrainVision RDA (Remote Data Access) API

'''

from ctypes import *

import numpy as np

import rdadefs as rda

__author__ = "Dmytro Bielievtsov"
__email__ = "belevtsoff@gmail.com"

def rda_read_start_msg(s, hdr):
    '''
    Reads an RDA start message from socket given the header
    
    Parameters
    ----------
    s : socket
        socket object
    hdr : rda_msg_hdr_t
        message header
    
    Returns
    -------
    mgs : rda_msg_start_full_t
        complete start message including variable fields
    
    '''
    # allocate space for the whole message
    buf = (c_char * hdr.nSize)()
    
    # create a view on the required part, no copies
    msg_fixed = rda.rda_msg_start_t.from_buffer(buf)
    msg_fixed.hdr = hdr
    
    # another view, cuz there's no offset argument in rect_into()
    rest = (c_char * (sizeof(msg_fixed) - sizeof(hdr)))\
            .from_buffer(buf, sizeof(hdr))
    
    n = s.recv_into(rest)
    check_received(n, rest)
    
    nChannels = msg_fixed.nChannels
    stringLength = sizeof(buf) - sizeof(msg_fixed) - nChannels * sizeof(c_double)
    
    # create new type including variable fields
    rda_msg_start_full_t = rda.rda_msg_start_t.full(nChannels, stringLength)
    
    # receive the rest
    # this may be a large block, so the receive command may release
    # before if received all the data. Using loop here until all
    # the data is received
    n = 0
    while n < rda_msg_start_full_t.varLength:
        msg_var = (c_char * (rda_msg_start_full_t.varLength - n)) \
                          .from_buffer(buf, sizeof(msg_fixed) + n)
        n += s.recv_into(msg_var)
    
    return rda_msg_start_full_t.from_buffer(buf)

def rda_read_data_msg(s, hdr, nChannels):
    '''
    Reads an RDA data message from socket given the header
    
    Parameters
    ----------
    s : socket
        socket object
    hdr : rda_msg_hdr_t
        message header
    nChannels : int
        number of channels (from start message)
    
    Returns
    -------
    msg : rda_msg_data_full_t
        complete data message including variable fields 
    
    '''
    # allocate space for the whole message
    buf = (c_char * hdr.nSize)()

    # create a view on the required part, no copies
    msg_fixed = rda.rda_msg_data_t.from_buffer(buf)
    msg_fixed.hdr = hdr
    
    # another view, cuz there's no offset argument in rect_into()
    rest = (c_char * (sizeof(msg_fixed) - sizeof(hdr)))\
            .from_buffer(buf, sizeof(hdr))
            
    n = s.recv_into(rest)
    check_received(n, rest)
    
    nPoints = msg_fixed.nPoints
    markersLength = sizeof(buf) - sizeof(msg_fixed) - nChannels * nPoints * sizeof(c_float)
    
    # create new type including variable fields
    rda_msg_data_full_t = rda.rda_msg_data_t.full(nChannels, nPoints, markersLength)
    
    # receive the rest
    # this may be a large block, so the receive command may release
    # before if received all the data. Using loop here until all
    # the data is received
    n = 0
    while n < rda_msg_data_full_t.varLength:
        msg_var = (c_char * (rda_msg_data_full_t.varLength - n)) \
                          .from_buffer(buf, sizeof(msg_fixed) + n)
        n += s.recv_into(msg_var)
    
    return rda_msg_data_full_t.from_buffer(buf)

def startmsg2string(msg):
    '''
    Converts an RDA start message (rda_msg_start_full_t) to string
    
    Parameters
    ----------
    msg : rda_msg_start_full_t:
        RDA start message
    
    Returns
    -------    
    string
    
    '''
    string = '%d channels: \n' % msg.nChannels
    sChannelNames = ubyte2string(msg.sChannelNames).split('\x00')
    dResolutions = np.frombuffer(msg.dResolutions, dtype=np.double)
    
    for chName, chRes in zip(sChannelNames, dResolutions):
        string += chName + ': %s uV\n' % chRes
    
    return string

def ubyte2string(array):
    '''
    Converts a ctypes ubyte array to string
    
    Parameters
    array : ctypes byte array
        input array
    
    Returns
    -------
    string
    
    '''
    return ''.join([chr(b) for b in array])

def check_received(n, msg):
    '''
    Checks whether the message is completely received. If OK, return
    nothing, else, raises an exception
    
    Parameters
    ----------
    n : int
        number of bytes received (returned by socket.recv_int())
    msg : ctypes structure
        message received
    
    '''
    if (n != sizeof(msg)):
        raise Exception('Failed to receive packet, received %s bytes,' + 
                        'should be %s bytes'
                        % (n, sizeof(msg)))
    else: pass
    
def validate_rda_guid(hdr):
    '''
    Checks whether the signature of the message is valid, given its header
    
    Parameters
    ----------
    hdr : rda_msg_hdr_t
        message header
    
    Returns
    -------
    result : bool
        verification result
    
    '''
    for b1, b2 in zip(hdr.guid, rda.RDA_GUID):
        if b1 != b2: return False
    return True
