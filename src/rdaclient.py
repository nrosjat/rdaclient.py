'''
RDA client classes. See rdaclient.Client's docstring for more information
'''

from multiprocessing import Process, Queue
from collections import deque
import signal
import ctypes as c
import socket
import logging
import time

import numpy as np

import rdadefs
import rdatools
import ringbuffer

__author__ = "Dmytro Bielievtsov"
__email__ = "belevtsoff@gmail.com"

class Client(object):
    '''
    An asynchronous RDA (Remote Data Access) client with buffer. Spawns a
    child process for storing constantly incoming data in the background.
    
    Currently supports only float32 data type
    
    Parameters
    ----------
    buffer_size : int, optional
        buffer capacity (in samples)
    buffer_window : int, optional
        buffer pocket size (in samples)
        
    Attributes
    ----------
    is_streaming
    buffer_size
    data_dtype
    buffer_window
    start_msg : None or rda_msg_start_full_t
        a start message obtained from the server after the first
        start_streaming() call.
        
    Notes
    -----
    The RDA data sharing is used by the BrainVision software.
    
    '''
    def __init__(self, buffer_size=300000, buffer_window=1):
        self.logger = logging.getLogger('rdaclient')
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        self.__buf = ringbuffer.RingBuffer()
        self.__buffer_size = buffer_size
        self.__data_dtype = 'float32' # for now
        self.__buffer_window = buffer_window
        
        self.__streamer = None
        self.q = Queue()
        
        self.start_msg = None
    
    def __get_is_streaming(self):
        try:
            return self.__streamer.is_alive()
        except:
            return False
    is_streaming = property(__get_is_streaming, None, None,
                            'Checks whether the Streamer is active, read-only (bool)')
    buffer_size = property(lambda self: self.__buffer_size, None, None,
                            'Buffer capacity in samples, read-only (int)')
    data_dtype = property(lambda self: self.__data_dtype, None, None,
                            'Buffer\'s data type, read-only (string)')
    buffer_window = property(lambda self: self.__buffer_window, None, None,
                            'Buffer pocket size, read-only (in samples)')
    last_sample = property(lambda self: self.__buf.nSamplesWritten, None, None,
                            'Number of a last sample written to the buffer\
                            (= total no.)')
    
    def connect(self, destaddr):
        '''
        Connects to an RDA server
        
        Parameters
        ----------        
        destaddr : tuple
            server address
        
        '''
        self.sock.connect(destaddr)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    
    def start_streaming(self, timeout=10):
        '''
        Starts data streaming from the server, using the following algorithm:
        
        1. Waits until start/data message arrives or timeout is over
        2. If start message arrived, initializes buffer
        3. Spawns a background process for data streaming
        4. Releases
        
        Parameters
        ----------
        timeout : float, optional
            time to wait for a start message (in seconds)
        
        '''
        
        if self.is_streaming:
            raise Exception('already streaming')
        
        self.logger.info('waiting for an rda start message...')
        
        hdr = rdadefs.rda_msg_hdr_t()
        
        then = time.time()
        now = time.time()
        while now - then < timeout:
            n = self.sock.recv_into(hdr)
            rdatools.check_received(n, hdr)
            
            if not rdatools.validate_rda_guid(hdr):
                self.logger.warning('packet with unknown GUID reveived')
                
            if hdr.nType == rdadefs.RDA_START_MSG:
                self.start_msg = rdatools.rda_read_start_msg(self.sock, hdr)
                self.logger.info('start message received, ' + \
                                 rdatools.startmsg2string(self.start_msg))
                break
            elif hdr.nType == rdadefs.RDA_FLOAT_MSG and self.start_msg:
                self.sock.recv(hdr.nSize - c.sizeof(hdr))
                self.logger.info('trying to resume previous session...')
                break
            else:
                self.sock.recv(hdr.nSize - c.sizeof(hdr))
                self.logger.info('skipped package (type = %s)' % hdr.nType)
            now = time.time()
        
        
        if not self.__buf.is_initialized:
            self.logger.info('initializing buffer...')
            self.__buf.initialize(int(self.start_msg.nChannels),
                                  self.buffer_size,
                                  self.buffer_window,
                                  self.data_dtype)
        
        self.logger.info('spawning a streamer process...')

        self.__streamer = Streamer(self.q, self.sock.fileno(), self.__buf.raw)
        self.__streamer._daemonic = True
        self.__streamer.start()
    
    def stop_streaming(self, write_timelog=False):
        '''
        Stops streaming by sending corresponding signal to a Streamer process
        
        Parameters
        ----------
        write_timelog : bool, optional
            If True, streamer will write its timelog to a file before
            stopping
        
        '''
        if not self.is_streaming:
            raise Exception('already stopped')
        
        self.q.put('stop')
        
        if write_timelog:
            self.q.put('save_timelog')
            
        self.__streamer.join()
        self.logger.info('stopped streaming')
        
    def disconnect(self):
        '''
        Disconnects the client from a server
        
        '''
        self.sock.close()
    
    def get_data(self, sampleStart, sampleEnd):
        '''
        Gets the data from the buffer. If possible, the data is returned in
        the form of a numpy view on the corresponding chunk (without copy)
        
        Parameters
        ----------
        sampleStart : int
            first sample index (included)
        sampleEnd : int
            last samples index (excluded)
        
        Returns
        -------
        data : ndarray (view or copy) or None
            data chunk or None, if the data is not available

        '''
        try:
            return self.__buf.get_data(sampleStart, sampleEnd)
        except:
            return None
    
    def wait(self, sampleStart, sampleEnd, timeout=1, sleep=5e-4):
        '''
        Gets the data from the buffer. Blocks if data is not available and
        releases if one of the following is true:
        
        1. data is available
        2. timeout is over
        3. data is overwritten
        
        Parameters
        ----------
        sampleStart : int
            first sample index (included)
        sampleEnd : int
            last samples index (excluded)
        timeout : float, optional
            timeout (seconds)
        sleep : float, optional
            time to wait until the next loop iteration. Used to avoid
            100% processor loading.
                            
        Returns
        -------
        data : ndarray (view or copy) or None
            data chunk or None, if the data is overwritten or the timeout
            has expired
                 
        '''
        
        if not self.is_streaming:
            raise Exception('nothing to wait, start streaming first')
        
        then = time.time()
        now = time.time()
        
        while now - then < timeout:
            try:
                return self.__buf.get_data(sampleStart, sampleEnd)
            except ringbuffer.BufferError as e:
                if e.code != 3: # if the data is overwritten
                    return None
                
            time.sleep(sleep)
            now = time.time()
            
        return None
    
    def poll(self, nSamples, timeout=10, sleep=0.0005):
        '''
        Gets the most resent data chunk from the buffer. Blocks until the
        next data block is written to the buffer or timeout is over.
        
        Parameters
        ----------
        nSamples : int
            chunk size (in samples)
        timeout : float
            timeout (seconds)
        sleep : float
            time to wait until the next loop iteration. Used to avoid
            100% processor loading.
                         
        Returns
        -------
        data : ndarray (view or copy) or None
            data chunk or None, if the data is overwritten or the timeout
            has expired
                 
        '''
        
        if not self.is_streaming:
            raise Exception('nothing to wait, start streaming first')
        
        ls = self.last_sample
        if self.wait(ls, ls + 1, timeout, sleep) is not None:
            ls = self.last_sample
            return self.get_data(ls - nSamples, ls)
        
        return None

#------------------------------------------------------------------------------ 
    
class Streamer(Process):
    '''
    A Streamer class. Inherited from the `multiprocessing.Process`. It is
    spawned by a Client to work in the background and receive the data.
    
    The buffer interface is initialized with a provided raw sharedctypes
    buffer array.
    
    Parameters
    ----------
    q : Queue
        Queue object for interprocess communication
    fd : int
        socket file descriptor (the one which is connected to a server)
    raw : sharectypes char array:
        a raw sharedctypes buffer array.
    '''
    def __init__(self, q, fd, raw):
        self.logger = logging.getLogger('data_streamer')
        self.sock = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
        self.__buf = ringbuffer.RingBuffer()
        self.__buf.initialize_from_raw(raw)
        self.q = q
        
        self.timelog = deque(maxlen=100000)
        self.timelog_fname = 'streamer_timelog'
        
        # dictionary of known commands
        self.cmds = {'save_timelog' : self.__save_timelog}
        
        super(Streamer, self).__init__()
       
    def run(self):
        '''
        The main streaming loop.
        
        '''
        cmd = self.__get_cmd()
        hdr = rdadefs.rda_msg_hdr_t()
        
        self.logger.info('started streaming')
        
        # Ignore Ctrl+C. The process is run in the daemonic mode, so if the
        # Client terminates, the streamer will be terminated anyway. This
        # ignoring however, allows for custom Ctrl+C handling in the
        # Client process to gracefully stop both the Client and the Streamer
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        
        # stream until there's a stop command
        while cmd != 'stop':
            n = self.sock.recv_into(hdr)
            rdatools.check_received(n, hdr)
            
            # check for a proper packet ID
            if not rdatools.validate_rda_guid(hdr):
                self.logger.warning('packet with unknown GUID reveived')
            
            if hdr.nType == rdadefs.RDA_FLOAT_MSG:
                msg = rdatools.rda_read_data_msg(self.sock, hdr, self.__buf.nChannels)
                self.__put_datablock(msg)
            
            # skip the weird undocumented package
            elif hdr.nType == 10000:
                self.sock.recv(hdr.nSize - c.sizeof(hdr))
            
            elif hdr.nType == rdadefs.RDA_STOP_MSG:
                self.logger.info('stop message received, stopping...')
                self.q.put('stop')
                time.sleep(.5)
                
            else:
                self.sock.recv(hdr.nSize - c.sizeof(hdr))
                self.logger.info('skipped package (type = %s)' % hdr.nType)
                
            cmd = self.__get_cmd()
                
        self.logger.info('stopped streaming')
        
        # execute the last command before exiting
        cmd = self.__get_cmd()
        self.__execute_cmd(cmd)
            
    
    def __put_datablock(self, msg):
        '''
        Reshapes the data chunk and pushes it to the buffer
         
        Parameters
        ----------            
        msg : rda_msg_data_t
            data message
        
        '''
        data = np.frombuffer(msg.fData, 'float32')
        self.__buf.put_data(np.reshape(data, (-1, self.__buf.nChannels)))
        self.timelog.append(time.time())
        self.logger.debug('put data: rda block #%s, %s samples, time: %.3f' % (msg.nBlock,
                                                                             msg.nPoints,
                                                                             self.timelog[-1]))
    
    def __get_cmd(self):
        '''
        Gets the command from the queue
        
        Returns
        -------        
        cmd : string or None, if there was no command
        
        '''
        try:
            cmd = self.q.get(False)
            return cmd
        except Exception:
            return None
        
    def __execute_cmd(self, cmd):
        '''
        Executes the command, if it's known
        
        Parameters
        ----------
        cmd : string
            command
        
        '''
        if self.cmds.has_key(cmd):
            try:
                self.cmds[cmd]()
            except:
                self.logger.warning('unable to execute command %s' % cmd)
    
    def __save_timelog(self):
        '''
        Saves the timelog to a file. The timelog contains data package
        arriving times. May be useful for debugging and network setup
        
        '''
        np.save(self.timelog_fname, np.array(self.timelog))


#------------------------------------------------------------------------------ 

logging.basicConfig(level=logging.INFO, format='[%(process)-5d:%(threadName)-10s] %(name)s: %(levelname)s: %(message)s')

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    client = Client(buffer_size=300000, buffer_window=10)
    client.connect(('', 51244))
    client.start_streaming()
    time.sleep(10)
    client.stop_streaming()
    client.disconnect()
    
    
