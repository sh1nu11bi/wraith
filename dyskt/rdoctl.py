#!/usr/bin/env python

""" rdoctl.py: Radio Controller

An interface to an underlying radio (wireless nic). Tunes and sniffs a radio
forwarding data to the RTO.
"""
__name__ = 'rdoctl'
__license__ = 'GPL v3.0'
__version__ = '0.0.8'
__date__ = 'July 2015'
__author__ = 'Dale Patterson'
__maintainer__ = 'Dale Patterson'
__email__ = 'wraith.wireless@yandex.com'
__status__ = 'Development'

import time                            # timestamps
import signal                          # handling signals
import socket                          # reading frames
import threading                       # for the tuner thread
from Queue import Queue, Empty         # thread-safe queue
import multiprocessing as mp           # for Process
from wraith.radio import iw            # iw command line interface
import wraith.radio.iwtools as iwt     # nic command line interaces
from wraith.radio.mpdu import MAX_MPDU # maximum size of frame

# tuner states
TUNE_DESC = ['scan','hold','pause','listen','stop']
TUNE_SCAN   = 0
TUNE_HOLD   = 1
TUNE_PAUSE  = 2
TUNE_LISTEN = 3
TUNE_STOP   = 4

class Tuner(threading.Thread):
    """ 'tunes' the radio's current channel and width """
    def __init__(self,q,conn,iface,scan,dwell,i,pause=False):
        """
         q: msg queue between radio and this Thread
         conn: dyskt token connnection
         iface: interface name of card
         scan: scan list of channel tuples (ch,chw)
         dwell: dwell list each stay on scan[i] for dwell[i]
         i: initial channel index
         pause: start paused?
        """
        threading.Thread.__init__(self)
        self._done = threading.Event() # the stop event
        self._qR = q                   # event queue from radio controller
        self._conn = conn              # connection from DySKT to tuner
        self._vnic = iface             # this radio's vnic
        self._chs = scan               # scan list
        self._ds = dwell               # corresponding dwell times
        self._i = i                    # initial/current index into scan/dwell
        self._state = TUNE_PAUSE if pause else TUNE_SCAN # initial state

    def shutdown(self): self._done.set()

    @property
    def currch(self): return '%d:%s' % (self._chs[self._i][0],self._chs[self._i][1])

    def run(self):
        """ switch channels based on associted dwell times """
        # starting paused or scanning?
        if self._state == TUNE_PAUSE:
            # notify rdoctl we are paused then hold on dyskt token q
            self._qR.put(('!PAUSE!',time.time(),(-1,' ')))
            self._conn.poll(None)
        else:
            self._qR.put(('!SCAN!',time.time(),(-1,self._chs)))

        # execution loop
        # wait on the internal connection for each channels dwell time.  IOT
        # avoid a state where we would continue to scan the same channel, i.e.
        # receiving repeated state commands, use a remaining time counter
        remaining = 0
        while not self._done.is_set():
            # set the dwell time to remaining if we need to finish this scan
            dwell = self._ds[self._i] if not remaining else remaining

            ts1 = time.time() # mark time
            if self._conn.poll(dwell):
                # get token and timestamp
                tkn = self._conn.recv()
                ts = time.time()

                # tokens will have 2 flavor's '!STOP!' and 'cmd:cmdid:params'
                # where params is empty or a '-' separated list
                if tkn == '!STOP!': self._qR.put(('!STOP!',ts,(-1,' ')))
                else:
                    # calculate time remaining for this channel
                    remaining = self._ds[self._i] - (ts-ts1)

                    # parse the requested command
                    try:
                        cmd,cid,ps = tkn.split(':') # force into 3 components
                        cid = int(cid)
                    except: # should never happen but just in case
                        self._qR.put(('!ERR!',ts,(-1,"invalid command format")))
                    else:
                        # for hold, pause & listen, notify rdoctl & then block
                        # on DySKT until we get another token (assumes DySKT wont
                        # send consecutive holds etc)
                        if cmd == 'state':
                            self._qR.put(('!STATE!',ts,(cid,TUNE_DESC[self._state])))
                            continue
                        elif cmd == 'scan':
                            if self._state != TUNE_SCAN:
                                self._state = TUNE_SCAN
                                self._qR.put(('!SCAN!',ts,(cid,self._chs)))
                                continue
                            else:
                                self._qR.put(('!ERR!',ts,(cid,'redundant command')))
                        elif cmd == 'txpwr': pass
                        elif cmd == 'spoof': pass
                        elif cmd == 'hold':
                            if self._state != TUNE_HOLD:
                                self._state = TUNE_HOLD
                                self._qR.put(('!HOLD!',ts,(cid,self.currch)))
                                self._conn.poll(None) # block until a new token
                                continue
                            else:
                                self._qR.put(('!ERR!',ts,(cid,'redundant command')))
                        elif cmd == 'pause':
                            if self._state != TUNE_PAUSE:
                                self._state = TUNE_PAUSE
                                self._qR.put(('!PAUSE!',ts,(cid,' ')))
                                self._conn.poll(None) # block until a new token
                                continue
                            else:
                                self._qR.put(('!ERR!',ts,(cid,'redundant command')))
                        elif cmd == 'listen':
                            # for listen, we ignore reduandacies as some other
                            # (external) process may have changed the channel
                            try:
                                ch,chw = ps.split('-')
                                iw.chset(self._vnic,ch,chw)
                                self._state = TUNE_LISTEN
                                self._qR.put(('!LISTEN',ts,(cid,"%s:%s" % (ch,chw))))
                                self._conn.poll(None) # block until a new token
                                continue
                            except ValueError:
                                self._qR.put(('!ERR!',ts,(cid,'invalid param format')))
                            except iw.IWException as e:
                                self._qR.put(('!ERR',ts,(cid,e)))
                        else:
                            self._qR.put(('!ERR!',ts,(cid,"invalid command %s" % cmd)))
            else:
                # no token, go to next channel
                try:
                    self._i = (self._i+1) % len(self._chs)
                    iw.chset(self._vnic,
                             str(self._chs[self._i][0]),
                             self._chs[self._i][1])
                    remaining = 0 # reset remaining
                except iw.IWException as e:
                    self._qR.put(('!FAIL!',time.time(),(-1,e)))
                except Exception as e: # blanket exception
                    self._qR.put(('!FAIL!',time.time(),(-1,e)))

class RadioController(mp.Process):
    """ Radio - primarily placeholder for radio details """
    def __init__(self,comms,conn,conf):
        """ initialize radio
         comms - internal communications
         conn - connection to/from DysKT
         conf - radio configuration dict. Must have key->value pairs for keys role,
          nic, dwell, scan and pass and optionally for keys spoof, ant_gain, ant_type,
          ant_loss, desc
         NOTE: the list of dwell times is an artifact of previous revisions. it
          is maintained here for future revisions that may implement adaptive
          scans
        """
        mp.Process.__init__(self)
        self._icomms = comms      # communications queue
        self._conn = conn         # message connection to/from DySKT
        self._qT = None           # queue between tuner and us
        self._role = None         # role this radio plays
        self._nic = None          # radio network interface controller name
        self._mac = None          # real mac address
        self._phy = None          # the phy of the device
        self._vnic = None         # virtual monitor name
        self._s = None            # the raw socket
        self._std = None          # supported standards
        self._chs = []            # supported channels
        self._txpwr = 0           # current tx power
        self._driver = None       # the driver
        self._chipset = None      # the chipset
        self._spoofed = ""        # spoofed mac address
        self._desc = None         # optional description
        self._tuner = None        # tuner thread
        self._stuner = None       # tuner state
        self._antenna = {'num':0, # antenna details
                         'gain':None,
                         'type':None,
                         'loss':None,
                         'x':None,
                         'y':None,
                         'z':None}
        self._setup(conf)

    def terminate(self): pass

    def run(self):
        """ run execution loop """
        # ignore signals being used by main program
        signal.signal(signal.SIGINT,signal.SIG_IGN)
        signal.signal(signal.SIGTERM,signal.SIG_IGN)

        # start tuner thread
        self._tuner.start()

        # execute sniffing loop
        while True:
            try:
                # check for any notifications from tuner thread
                # a tuple t=(event,timestmap,message) where message is another
                # tuple m=(cmdid,description) such that cmdid != -1 if this
                # command comes from an external source
                event,ts,msg = self._qT.get_nowait()
            except Empty:
                # no notices from tuner thread
                try:
                    # pull the frame off and pass it on
                    frame = self._s.recv(MAX_MPDU)
                    if self._stuner == TUNE_PAUSE: continue # don't forward
                    self._icomms.put((self._vnic,time.time(),'!FRAME!',frame))
                except socket.timeout: continue
                except socket.error as e:
                    self._icomms.put((self._vnic,time.time(),'!FAIL!',e))
                    self._conn.send(('err',self._role,'Socket',e,))
                    break
                except Exception as e:
                    # blanket 'don't know what happend' exception
                    self._icomms.put((self._vnic,time.time(),'!FAIL!',e))
                    self._conn.send(('err',self._role,'Unknown',e))
                    break
            else:
                # process the notification
                if event == '!ERR!':
                    # bad/failed command from user, notify DySKT
                    if msg[0] > -1: self._conn.send(('cmderr',self._role,msg[0],msg[1]))
                elif event == '!FAIL!':
                    self._icomms.put((self._vnic,ts,'!FAIL!',msg[1]))
                elif event == '!STATE!':
                    if msg[0] > -1: self._conn.send(('cmdack',self._role,msg[0],msg[1]))
                elif event == '!HOLD!':
                    self._stuner = TUNE_HOLD
                    self._icomms.put((self._vnic,ts,'!HOLD!',msg[1]))
                    if msg[0] > -1: self._conn.send(('cmdack',self._role,msg[0],msg[1]))
                elif event == '!SCAN!':
                    self._stuner = TUNE_SCAN
                    self._icomms.put((self._vnic,time.time(),'!SCAN!',msg[1]))
                    if msg[0] > -1: self._conn.send(('cmdack',self._role,msg[0],msg[1]))
                elif event == '!LISTEN':
                    self._stuner = TUNE_LISTEN
                    self._icomms.put((self._vnic,time.time(),'!LISTEN!',msg[1]))
                    if msg[0] > -1: self._conn.send(('cmdack',self._role,msg[0],msg[1]))
                elif event == '!PAUSE!':
                    self._stuner = TUNE_PAUSE
                    self._icomms.put((self._vnic,time.time(),'!PAUSE!',' '))
                    if msg[0] > -1: self._conn.send(('cmdack',self._role,msg[0],msg[1]))
                elif event == '!STOP!':
                    self._stuner = TUNE_STOP
                    break

        # shut down
        if not self.shutdown():
            try:
                self._conn.send(('warn',self._role,'Shutdown',"Incomplete reset"))
            except IOError:
                # most likely DySKT already closed their side
                pass

    def shutdown(self):
        """
         restore everything & clean up. returns whether a full reset or not occurred
        """
        # try shutting down & resetting radio (if it failed we may not be able to)
        clean = True
        try:
            # stop the tuner
            try:
                # call shutdown and join timing out if necessary
                self._tuner.shutdown()
                self._tuner.join()
            except:
                clean = False
            else:
                self._tuner = None

            # reset the device
            try:
                iw.devdel(self._vnic)
                iw.phyadd(self._phy,self._nic)
                if self._spoofed:
                    iwt.ifconfig(self._nic,'down')
                    iwt.resethwaddr(self._nic)
                iwt.ifconfig(self._nic,'up')
            except iw.IWException:
                clean = False

            # close socket and connection
            if self._s:
                self._s.shutdown(socket.SHUT_RD)
                self._s.close()
            self._conn.close()
        except:
            clean = False
        return clean

    @property
    def radio(self):
        """ returns a dict describing this radio """
        return {'nic':self._nic,
                'vnic':self._vnic,
                'phy':self._phy,
                'mac':self._mac,
                'role':self._role,
                'spoofed':self._spoofed,
                'driver':self._driver,
                'chipset':self._chipset,
                'standards':self._std,
                'channels':self._chs,
                'txpwr':self._txpwr,
                'desc':self._desc,
                'nA':self._antenna['num'],
                'type':self._antenna['type'],
                'gain':self._antenna['gain'],
                'loss':self._antenna['loss'],
                'x':self._antenna['x'],
                'y':self._antenna['y'],
                'z':self._antenna['z']}

    def _setup(self,conf):
        """
         1) sets radio properties as specified in conf
         2) prepares specified nic for monitoring and binds it
         3) creates a scan list and compile statistics
        """
        # if the nic specified in conf is present, set it
        if conf['nic'] not in iwt.wifaces():
            raise RuntimeError("%s:iwtools.wifaces:not found" % conf['role'])
        self._nic = conf['nic']
        self._role = conf['role']

        # get the phy and associated interfaces
        try:
            (self._phy,ifaces) = iw.dev(self._nic)
            self._mac = ifaces[0]['addr']
        except (KeyError, IndexError):
            raise RuntimeError("%s:iw.dev:error getting interfaces" % self._role)
        except iw.IWException as e:
            raise RuntimeError("%s:iw.dev:failed to get phy <%s>" % (self._role,e))

        # get properties (the below will return None rather than throw exception)
        self._chs = iw.chget(self._phy)
        self._std = iwt.iwconfig(self._nic,'Standards')
        self._txpwr = iwt.iwconfig(self._nic,'Tx-Power')
        self._driver = iwt.getdriver(self._nic)
        self._chipset = iwt.getchipset(self._driver)

        # spoof the mac address ??
        if conf['spoofed']:
            mac = None if conf['spoofed'].lower() == 'random' else conf['spoofed']
            try:
                iwt.ifconfig(self._nic,'down')
                self._spoofed = iwt.sethwaddr(self._nic,mac)
            except iwt.IWToolsException as e:
                raise RuntimeError("%s:iwtools.sethwaddr:%s" % (self._role,e))

        # delete all associated interfaces - we want full control
        for iface in ifaces: iw.devdel(iface['nic'])

        # determine virtual interface name
        ns = []
        for wiface in iwt.wifaces():
            cs = wiface.split('dyskt')
            try:
                if len(cs) > 1: ns.append(int(cs[1]))
            except ValueError:
                pass
        n = 0 if not 0 in ns else max(ns)+1
        self._vnic = "dyskt%d" % n

        # sniffing interface
        try:
            iw.phyadd(self._phy,self._vnic,'monitor') # create a monitor,
            iwt.ifconfig(self._vnic,'up')             # and turn it on
        except iw.IWException as e:
            # never added virtual nic, restore nic
            errMsg = "%s:iw.phyadd:%s" % (self._role,e)
            try:
                iwt.ifconfig(self._nic,'up')
            except iwt.IWToolsException:
                errMsg += " Failed to restore %s" % self._nic
            raise RuntimeError(errMsg)
        except iwt.IWToolsException as e:
            # failed to 'raise' virtual nic, remove vnic and add nic
            errMsg = "%s:iwtools.ifconfig:%s" % (self._role,e)
            try:
                iw.phyadd(self._phy,self._nic,'managed')
                iw.devdel(self._vnic)
                iwt.ifconfig(self._nic,'up')
            except (iw.IWException,iwt.IWToolsException):
                errMsg += " Failed to restore %s" % self._nic
            raise RuntimeError(errMsg)

        # wrap remaining in a try block, we must attempt to restore card and
        # release the socket after any failures ATT
        self._s = None
        try:
            # bind socket w/ a timeout of 5 just in case there is no wireless traffic
            self._s = socket.socket(socket.AF_PACKET,
                                    socket.SOCK_RAW,
                                    socket.htons(0x0003))
            self._s.settimeout(5)
            self._s.bind((self._vnic,0x0003))
            uptime = time.time()

            # read in antenna details and radio description
            if conf['antennas']['num'] > 0:
                self._antenna['num'] = conf['antennas']['num']
                self._antenna['type'] = conf['antennas']['type']
                self._antenna['gain'] = conf['antennas']['gain']
                self._antenna['loss'] = conf['antennas']['loss']
                self._antenna['x'] = [v[0] for v in conf['antennas']['xyz']]
                self._antenna['y'] = [v[1] for v in conf['antennas']['xyz']]
                self._antenna['z'] = [v[2] for v in conf['antennas']['xyz']]
            self._desc = conf['desc']

            # compile initial scan pattern from config
            scan = [t for t in conf['scan'] if str(t[0]) in self._chs and not t in conf['pass']]

            # sum hop times with side effect of removing any invalid channel tuples
            # i.e. Ch 14, Width HT40+ and channels the card cannot tune to
            i = 0
            hop = 0
            while scan:
                try:
                    t = time.time()
                    iw.chset(self._vnic,str(scan[i][0]),scan[i][1])
                    hop += (time.time() - t)
                    i += 1
                except iw.IWException as e:
                    # if invalid argument, drop the channel otherwise reraise error
                    if iw.ecode(str(e)) == iw.IW_INVALIDARG:
                        del scan[i]
                    else:
                        raise
                except IndexError:
                    # all channels checked
                    break
            if not scan: raise ValueError("Empty scan pattern")


            # calculate avg hop time, and interval time
            # NOTE: these are not currently used but may be useful later
            hop /= len(scan)
            interval = len(scan) * conf['dwell'] + len(scan) * hop

            # create list of dwell times
            ds = [conf['dwell']] * len(scan)

            # get start ch & set the initial channel
            try:
                ch_i = scan.index(conf['scan_start'])
            except ValueError:
                ch_i = 0
            iw.chset(self._vnic,str(scan[ch_i][0]),scan[ch_i][1])

            # initialize tuner thread
            self._qT = Queue()
            self._tuner = Tuner(self._qT,self._conn,self._vnic,scan,ds,ch_i,conf['paused'])

            # notify RTO we are good
            self._icomms.put((self._vnic,uptime,'!UP!',self.radio))
        except socket.error as e:
            try:
                iw.devdel(self._vnic)
                iw.phyadd(self._phy,self._nic)
                iwt.ifconfig(self._nic,'up')
            except (iw.IWException,iwt.IWToolsException):
                pass
            if self._s:
                self._s.shutdown(socket.SHUT_RD)
                self._s.close()
            raise RuntimeError("%s:Raw Socket:%s" % (self._role,e))
        except iw.IWException as e:
            try:
                iw.devdel(self._vnic)
                iw.phyadd(self._phy,self._nic)
                iwt.ifconfig(self._nic,'up')
            except (iw.IWException,iwt.IWToolsException):
                pass
            if self._s:
                self._s.shutdown(socket.SHUT_RD)
                self._s.close()
            raise RuntimeError("%s:iw.chset:Failed to set channel: %s" % (self._role,e))
        except (ValueError,TypeError) as e:
            try:
                iw.devdel(self._vnic)
                iw.phyadd(self._phy,self._nic)
                iwt.ifconfig(self._nic,'up')
            except (iw.IWException,iwt.IWToolsException):
                pass
            if self._s:
                self._s.shutdown(socket.SHUT_RD)
                self._s.close()
            raise RuntimeError("%s:config:%s" % (self._role,e))
        except Exception as e:
            # blanket exception
            try:
                iw.devdel(self._vnic)
                iw.phyadd(self._phy,self._nic)
                iwt.ifconfig(self._nic,'up')
            except (iw.IWException,iwt.IWToolsException):
                pass
            if self._s:
                self._s.shutdown(socket.SHUT_RD)
                self._s.close()
            raise RuntimeError("%s:Unknown:%s" % (self._role,e))