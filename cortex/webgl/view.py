import os
import re
import glob
import json
import shutil
import random
import binascii
import mimetypes
import functools
import webbrowser
import multiprocessing as mp
import numpy as np

from tornado import web, template

from .. import utils, options

from . import serve

sloader = template.Loader(serve.cwd)
lloader = template.Loader("./")

name_parse = re.compile(r".*/(\w+).png")
try:
    cmapdir = options.config.get('webgl', 'colormaps')
except:
    cmapdir = os.path.join(options.config.get("basic", "filestore"), "colormaps")
colormaps = glob.glob(os.path.join(cmapdir, "*.png"))
colormaps = [(name_parse.match(cm).group(1), serve.make_base64(cm)) for cm in sorted(colormaps)]

def _normalize_data(data, pfunc):
    from scipy.stats import scoreatpercentile
    if not isinstance(data, dict):
        data = dict(data0=data)

    json = dict()
    json['__order__'] = list(data.keys())
    for name, dat in list(data.items()):
        ds = dict(__class__="Dataset")
        mapper = pfunc()[1]
        if 'projection' in dat:
            mapper = pfunc(projection=dat['projection'])[1]

        if isinstance(dat, dict):
            data = _fixarray(dat['data'], mapper)
            if 'stim' in dat:
                ds['stim'] = dat['stim']
            ds['delay'] = dat['delay'] if 'delay' in dat else 0
        else:
            data = _fixarray(dat, mapper)

        ds['data'] = data
        ds['min'] = float(scoreatpercentile(data.ravel(), 1) if 'min' not in dat else dat['min'])
        ds['max'] = float(scoreatpercentile(data.ravel(), 99) if 'max' not in dat else dat['max'])
        if 'cmap' in dat:
            ds['cmap'] = dat['cmap']
        if 'rate' in dat:
            ds['rate'] = dat['rate']

        json[name] = ds

    return json

def _make_bindat(json, fmt="%s.bin"):
    newjs, bindat = dict(), dict()
    for name, data in list(json.items()):
        if isinstance(data, dict):
            newjs[name] = data.copy()
            newjs[name]['data'] = fmt%name
            bindat[name] = serve.make_binarray(data['data'])
        else:
            newjs[name] = data

    return newjs, bindat

def _fixarray(data, mapper):
    if isinstance(data, str):
        if os.path.splitext(data)[1] in ('.hdf', '.mat'):
            try:
                import tables
                data = tables.openFile(data).root.data[:]
            except IOError:
                import scipy.io as sio
                data = sio.loadmat(data)['data'].T
        elif '.nii' in data:
            import nibabel
            data = nibabel.load(data).get_data().T
    if data.dtype != np.uint8:
        data = data.astype(np.float32)

    raw = data.dtype.type == np.uint8
    mapped = mapper.nverts in data.shape

    if raw:
        assert mapped and data.shape[-2] in (3, 4)
        if data.shape[-2] == 3:
            if data.ndim == 2:
                data = np.vstack([data, 255*np.ones((1, mapper.nverts), dtype=np.uint8)])
            else:
                data = np.hstack([data, 255*np.ones((len(data), 1, mapper.nverts), dtype=np.uint8)])

        data = np.hstack(mapper(data.reshape(-1, mapper.nverts)))
        if data.shape[-2] != 4:
            data = data.reshape(-1, 4, mapper.nverts)
        return data.swapaxes(-1, -2)
    else: #regular
        return np.hstack(mapper(data)).astype(np.float32)

def make_movie(stim, outfile, fps=15, size="640x480"):
    import shlex
    import subprocess as sp
    cmd = "ffmpeg -r {fps} -i {infile} -b 4800k -g 30 -s {size} -vcodec libtheora {outfile}.ogv"
    fcmd = cmd.format(infile=stim, size=size, fps=fps, outfile=outfile)
    sp.call(shlex.split(fcmd))

def make_static(outpath, data, subject, xfmname, types=("inflated",), projection='nearest', recache=False, cmap="RdBu_r", template="static.html", anonymize=False, **kwargs):
    """
    Creates a static instance of the webGL MRI viewer that can easily be posted 
    or shared. 

    Parameters
    ----------
    outpath : string
        The directory where the static viewer will be saved. Will be created if it
        doesn't already exist.
    data : array_like or dict
        The data to be displayed on the surface. For details see docs for show().
    subject : string
        Subject identifier (e.g. "JG").
    xfmname : string
        Name of anatomical -> functional transform.
    types : tuple, optional
        Types of surfaces to include. Fiducial and flat surfaces are automatically
        included. Default ("inflated",)
    recache : bool, optional
        Whether to recreate CTM and SVG files for surfaces. Default False
    cmap : string, optional
        Name of default colormap used to show data. Default "RdBu_r"
    template : string, optional
        Name of template HTML file. Default "static.html"
    anonymize : bool, optional
        Whether to rename CTM and SVG files generically, for public distribution.
        Default False
    **kwargs : dict, optional
        All additional keyword arguments are passed to the template renderer.

    You'll probably need nginx to view this, since file:// paths don't handle xsrf correctly
    """
    outpath = os.path.abspath(os.path.expanduser(outpath)) # To handle ~ expansion
    if not os.path.exists(outpath):
        os.makedirs(outpath)

    #Create a new mg2 compressed CTM and move it into the outpath
    pfunc = functools.partial(utils.get_ctmpack, subject, xfmname, types, projection=projection, method='mg2', level=9)
    ctmfile, mapper = pfunc(recache=recache)
    oldpath, fname = os.path.split(ctmfile)
    fname, ext = os.path.splitext(fname)

    ## Rename files to anonymize?
    if anonymize:
        newfname = "surface"
    else:
        newfname = fname

    for ext in ['json','ctm', 'svg']:
        newfile = os.path.join(outpath, "%s.%s"%(newfname, ext))
        if os.path.exists(newfile):
            os.unlink(newfile)
        
        shutil.copy2(os.path.join(oldpath, "%s.%s"%(fname, ext)), newfile)

        if ext == "json" and anonymize:
            ## change filenames in json
            nfh = open(newfile)
            jsoncontents = nfh.read()
            nfh.close()
            
            ofh = open(newfile, "w")
            ofh.write(jsoncontents.replace(fname, newfname))
            ofh.close()

    #ctmfile = os.path.split(ctmfile)[1]
    ctmfile = newfname+".json"

    #Generate the data binary objects and save them into the outpath
    json, sdat = _make_bindat(_normalize_data(data, pfunc))
    for name, dat in list(sdat.items()):
        with open(os.path.join(outpath, "%s.bin"%name), "wb") as binfile:
            binfile.write(dat)
    
    #Parse the html file and paste all the js and css files directly into the html
    from . import htmlembed
    if os.path.exists(os.path.join("./", template)):
        template = lloader.load(template)
    else:
        template = sloader.load(template)
    html = template.generate(ctmfile=ctmfile, data=json, colormaps=colormaps, default_cmap=cmap, python_interface=False, **kwargs)
    htmlembed.embed(html, os.path.join(outpath, "index.html"))
    return mapper

def show(data, subject, xfmname, types=("inflated",), projection='nearest', recache=False, recache_mapper=False, cmap="RdBu_r", autoclose=True, open_browser=True, port=None, pickerfun=None, **kwargs):
    """Data can be a dictionary of arrays. Alternatively, the dictionary can also contain a 
    sub dictionary with keys [data, stim, delay].

    Data array can be a variety of shapes:
    Regular volume movie: [t, z, y, x]
    Regular volume image: [z, y, x]
    Regular masked movie: [t, vox]
    Regular masked image: [vox]
    Regular vertex movie: [t, verts]
    Regular vertex image: [verts]
    Raw vertex movie:     [[3, 4], t, verts]
    Raw vertex image:     [[3, 4], verts]
    """
    html = sloader.load("mixer.html")
    pfunc = functools.partial(utils.get_ctmpack, subject, xfmname, types, projection=projection, method='mg2', level=9)
    ctmfile, mapper = pfunc(recache=recache, recache_mapper=recache_mapper)
    jsondat, bindat = _make_bindat(_normalize_data(data, pfunc), fmt='data/%s/')

    saveevt = mp.Event()
    saveimg = mp.Array('c', 8192)
    queue = mp.Queue()
    
    class CTMHandler(web.RequestHandler):
        def get(self, path):
            fpath = os.path.split(ctmfile)[0]
            if path == '':
                self.set_header("Content-Type", "application/json")
                self.write(open(ctmfile).read())
            else:
                mtype = mimetypes.guess_type(os.path.join(fpath, path))[0]
                if mtype is None:
                    mtype = "application/octet-stream"
                self.set_header("Content-Type", mtype)
                self.write(open(os.path.join(fpath, path)).read())

    class DataHandler(web.RequestHandler):
        def get(self, path):
            path = path.strip("/")
            try:
                d = queue.get(True, 0.1)
                print("Got new data: %r"%list(d.keys()))
                bindat.update(d)
            except:
                pass

            if path in bindat:
                self.write(bindat[path])
            else:
                self.set_status(404)
                self.write_error(404)

    class MixerHandler(web.RequestHandler):
        def get(self):
            self.set_header("Content-Type", "text/html")
            self.write(html.generate(data=jsondat, colormaps=colormaps, default_cmap=cmap, python_interface=True))

        def post(self):
            print("saving file to %s"%saveimg.value)
            data = self.get_argument("svg", default=None)
            png = self.get_argument("png", default=None)
            with open(saveimg.value, "wb") as svgfile:
                if png is not None:
                    data = png[22:].strip()
                    try:
                        data = binascii.a2b_base64(data)
                    except:
                        print("Error writing image!")
                        data = png
                svgfile.write(data)
            saveevt.set()

    if pickerfun is None:
        pickerfun = lambda a,b: None

    class JSLocalMixer(serve.JSLocal):
        def addData(self, **kwargs):
            Proxy = serve.JSProxy(self.send, "window.viewer.addData")
            json, data = _make_bindat(_normalize_data(kwargs, pfunc), fmt='data/%s/')
            queue.put(data)
            return Proxy(json)

    class JSMixer(serve.JSProxy):
        def addData(self, **kwargs):
            Proxy = serve.JSProxy(self.send, "window.viewer.addData")
            json, data = _make_bindat(_normalize_data(kwargs, pfunc), fmt='data/%s/')
            queue.put(data)
            return Proxy(json)

        def saveflat(self, filename, height=1024):
            Proxy = serve.JSProxy(self.send, "window.viewer.saveflat")
            saveimg.value = filename
            return Proxy(height, "mixer.html")

        def saveIMG(self, filename):
            Proxy = serve.JSProxy(self.send, "window.viewer.saveIMG")
            saveimg.value = filename
            return Proxy("mixer.html")

        def makeMovie(self, animation, filename="brainmovie%07d.png", fps=30, shape=(1920, 1080)):
            state = dict()
            anim = []
            for f in sorted(animation, key=lambda x:x['idx']):
                if f['idx'] == 0:
                    self.setState(f['state'], f['value'])
                    state[f['state']] = dict(idx=f['idx'], val=f['value'])
                else:
                    if f['state'] not in state:
                        state[f['state']] = dict(idx=0, val=self.getState(f['state'])[0])
                    start = dict(idx=state[f['state']]['idx'], state=f['state'], value=state[f['state']]['val'])
                    end = dict(idx=f['idx'], state=f['state'], value=f['value'])
                    state[f['state']]['idx'] = f['idx']
                    state[f['state']]['val'] = f['value']
                    if start['value'] != end['value']:
                        anim.append((start, end))

            print(anim)
            self.resize(*shape)
            for i, sec in enumerate(np.arange(0, anim[-1][1]['idx'], 1./fps)):
                for start, end in anim:
                    if start['idx'] < sec < end['idx']:
                        idx = (sec - start['idx']) / (end['idx'] - start['idx'])
                        val = np.array(start['value']) * (1-idx) + np.array(end['value']) * idx
                        if isinstance(val, np.ndarray):
                            self.setState(start['state'], list(val))
                        else:
                            self.setState(start['state'], val)
                saveevt.clear()
                self.saveIMG(filename%i)
                saveevt.wait()

    class PickerHandler(web.RequestHandler):
        def initialize(self, server):
            self.client = JSLocalMixer(server.srvsend, server.srvresp)

        def get(self):
            pickerfun(self.client, int(self.get_argument("voxel")), int(self.get_argument("vertex")))

    class WebApp(serve.WebApp):
        disconnect_on_close = autoclose
        def get_client(self):
            self.c_evt.wait()
            self.c_evt.clear()
            return JSMixer(self.send, "window.viewer")

        def get_local_client(self):
            return JSMixer(self.srvsend, "window.viewer")

    if port is None:
        port = random.randint(1024, 65536)
        
    srvdict = dict()
    server = WebApp([
            (r'/ctm/(.*)', CTMHandler),
            (r'/data/(.*)', DataHandler),
            (r'/mixer.html', MixerHandler),
            (r'/picker', PickerHandler, srvdict),
            (r'/', MixerHandler),
        ], port)
    srvdict['server'] = server
    server.start()
    print("Started server on port %d"%server.port)
    if open_browser:
        webbrowser.open("http://%s:%d/mixer.html"%(serve.hostname, server.port))

        client = server.get_client()
        client.server = server
        return client
