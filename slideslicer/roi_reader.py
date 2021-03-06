import re
import json
import numpy as np
import pandas as pd
import openslide
import shapely
from shapely import affinity
from shapely.geometry import Polygon, MultiPolygon, MultiLineString, GeometryCollection
from descartes import PolygonPatch
import matplotlib.pyplot as plt
from matplotlib import colors
from itertools import cycle
from functools import partial, reduce
from warnings import warn
from .slideutils import (get_vertices, get_roi_dict, get_median_color,
                        get_threshold_tissue_mask, convert_mask2contour,
                        get_thumbnail_magnification, plot_contour)

from .parse_leica_xml import parse_xml2annotations
from .geom_tools import resolve_selfintersection, get_ellipse_verts_from_bbox
from .slideutils import sample_points, CentredRectangle



class ROIFrame(pd.DataFrame):
    def __init__(self, *args, **kwargs):
        pd.DataFrame.__init__(self, *args, **kwargs)

    def to_dict(self, orient='records'):
        return pd.DataFrame.to_dict(self, orient=orient)

    def to_json(self, path_or_buf=None, orient='records',
                lines=False, compression=None, index=True,
                **kwargs):
        return pd.DataFrame.to_json(self, orient=orient,
                lines=lines, compression=compression, index=index,
                **kwargs)

    def __getitem__(self, key):
        data = pd.DataFrame.__getitem__(self, key)
        if isinstance(data, pd.DataFrame):
            return ROIFrame(data)
        else:
            return data
        

def _get_patch_(slide, xc, yc,
              patch_size = [1024, 1024],
              magn_base = 4,
              scale = 2,
              use_cached=True,
             ):
    """retrieve a patch from openslide with given center point, size, and subsampling rate
    currently tested only on Leica SVS slides"""
    if scale>0:
        target_subsample = max(scale, 1/scale)
    else:
        target_subsample = - scale
    exp_raw = np.log2(target_subsample)/np.log2(magn_base)
    if use_cached:
        magn_exp = int(np.floor(exp_raw))
    else:
        magn_exp = 0
    subsample = magn_base**-(exp_raw-magn_exp)

    size_ = [ps//(magn_base**magn_exp) for ps in patch_size]
    region_ = slide.read_region((int(xc-patch_size[0]//2), int(yc-patch_size[1]//2)), magn_exp, size_)
    if subsample!= 1.0:
        region_ = region_.resize([int(subsample * s) for s in region_.size], 
                                 openslide.Image.ANTIALIAS)
        #region_ = np.asarray(region_)[...,:3]
        #region_ = cv2.resize(region_, (0,0), fx=subsample, fy=subsample,
        #                        interpolation = cv2.INTER_AREA)
    return region_


def find_chunk_content(roilist):
    """finds features (gloms, infl, etc) contained within tissue chunks.
    Returns a dictionary:
    {tissue_chunk_1_id: [feature_1_id, ..., feature_n_id],
     tissue_chunk_1_id: [...]
    }
    Requires `shapely` package
    """
    pgs_tissue = {}
    pgs_feature = {}
    for roi in roilist:
        try:
            if roi["name"]=="tissue":
                pgs_tissue[roi['name']] = Polygon(roi["vertices"])
            else:
                pgs_feature[roi['name']] = Polygon(roi["vertices"])
        except ValueError as ee:
            warn(str(ee))
            continue

    tissue_contains = dict(zip(pgs_tissue.keys(), [[] for _ in range(len(pgs_tissue))]))
    remove_items = []
    for idt, pt in pgs_tissue.items():
        for idf in remove_items:
            pgs_feature.pop(idf)
        remove_items = []
        for idf, pf in pgs_feature.items():
            if pt.intersects(pf):
                remove_items.append(idf)
                tissue_contains[idt].append(idf)
    return tissue_contains


def remove_empty_tissue_chunks(roilist):
    """removes tissue chunks that contain no annotation contours within"""
    chunk_content = find_chunk_content(roilist)
    empty_chunks = set([kk for kk,vv in chunk_content.items() if len(vv)==0])
    return [roi for roi in roilist if roi['name'] not in empty_chunks]


class RoiReader():
    """a generic class for matched annotations-slide reading and processing 
    """
    def __init__(self, inputfile,
                  annotation_file=None,
                  threshold_tissue=True, remove_empty=True,
                  threshold_color=False,
                  alt_annotation_file=None,
                  save=True, outdir=None, minlen=50,
                  annotation_format='leica',
                  slide_format='leica',
                  verbose=True):
        """
        extract and save rois

        Inputs:
        inputfile     -- whole slide imaging file path
        remove_empty  -- remove empty chunks of tissue
                          - True:   remove
                          - False:  don't remove
                          - None:   remove only if other rois are present
        outdir        -- (optional); save into an alternative directory
        minlen        -- minimal length of tissue chunk contour in thumbnail image
        keeplevels    -- number of file path elements to keep 
                         when saving to provided `outdir`
                         (1 -- filename only; 2 -- incl 1 directory)
        """
	self.inputfile = inputfile
        self.filenamebase = re.sub('.(svs|tif)$','', re.sub(".xml$", "", inputfile))             ######### Place changed 
        self.verbose = verbose
        ############################
        # parsing annotations
        ############################
        self.slide_format = slide_format
        if annotation_format == 'leica':
            if alt_annotation_file:
                fnxml = alt_annotation_file
            else:
                fnxml = self.filenamebase + '.xml'
            try:
                self.rois = parse_xml2annotations(fnxml)
                for roi in self.rois:
                    roi['name'] = roi['name']
            except:
                warn('ROI file not found;\nexpected:\t{}'.format(fnxml))
        else:
            NotImplementedError('format "%s" is not supported yet' % annotation_format)
            
        # for an ellipse, 
        #    area = $\pi \times r \times R$

        if threshold_tissue:
            self.add_tissue(remove_empty=remove_empty,
                            color=threshold_color, filtersize=7, minlen=minlen)

        if save:
            self.save()

    @property 
    def thumbnail(self):
        return self.load_thumbnail()

    def load_thumbnail(self):
        slide = self.slide
        #self.img = np.asarray(slide.associated_images["thumbnail"])
        self.img = np.asarray(slide.get_thumbnail((500,500)))
        self.width, self.height = slide.dimensions
        self.median_color = get_median_color(slide)
        self._thumbnail_ratio = get_thumbnail_magnification(slide)
        return self.img

    @property
    def slide(self):
        slide_ = openslide.OpenSlide(self.inputfile)
        return slide_


    def extract_tissue(self, color=False, filtersize=7, minlen=50):
        ## Extract tissue chunk ROIs
        self.load_thumbnail()

        ## Extract mask and contours
        mask = get_threshold_tissue_mask(self.img, color=color, filtersize=filtersize)
        contours = convert_mask2contour(mask, minlen=minlen)

        # if hasattr(self,'rois'):
        #     sq_micron_per_pixel = np.median([roi["areamicrons"] / roi["area"] 
        #                                     for roi in self.rois])
        #     len_rois = len(self.rois)
        # else:
        sq_micron_per_pixel = None
        len_rois = 0

        self.tissue_rois = [get_roi_dict(cc*self._thumbnail_ratio,
                                        name='tissue', id=1+nn+len_rois,
                                        sq_micron_per_pixel=sq_micron_per_pixel) 
                            for nn,cc in enumerate(contours)]
        return self.tissue_rois 


    def add_tissue(self, remove_empty=True,
                   color=False, filtersize=7, minlen=50):
        '''Inputs:
        - remove_empty: 
            True:   remove
            False:  don't remove
            None:   remove only if other rois are present
        - color       -- color-based thresholding to obtain tissue contours
        - filtersize  -- size of the median filter
        - minlen      -- minimal tissue contour length
        '''
                   
        if not hasattr(self, 'tissue_rois'):
            self.extract_tissue(color=color, filtersize=filtersize, minlen=minlen) 

        if hasattr(self,'rois'):
            self.rois = self.rois + self.tissue_rois
        else:
            self.rois = self.tissue_rois

        if self.verbose:
            print('-'*15)
            print("counts of ROIs")
            print('-'*15)
            roi_name_counts = (pd.Series([rr["name"] for rr in self.rois])
                              .value_counts() )
            print(roi_name_counts)
        
        if remove_empty:
            self.rois = remove_empty_tissue_chunks(self.rois)
            if self.verbose:
                print('removing empty', remove_empty)
        elif (remove_empty is None) and sum((rr['name']!='tissue' for rr in self.rois))>0:
            self.rois = remove_empty_tissue_chunks(self.rois)
            if self.verbose:
                print('removing empty', remove_empty)

        if self.verbose and remove_empty is not False:
            print('-'*45)
            print("counts of ROIs after removing empty chunks")
            print('-'*45)
            roi_name_counts = pd.Series([rr["name"] for rr in self.rois]).value_counts()
            print(roi_name_counts)


    @property
    def df(self):
        if not hasattr(self, '_df'):
            self._df = pd.DataFrame(self.rois)
            if len(self.rois) == 0:
                self._df = pd.DataFrame({'name':[], 'type':[], 'polygon':[], 'vertices': []})
                return self._df
            mask_ellipse = self._df['type']==2
            self._df.loc[mask_ellipse, 'vertices'] = \
                self._df.loc[mask_ellipse,'vertices'].map(partial(get_ellipse_verts_from_bbox, points=50))
            self._df['polygon'] = self._df['vertices'].map(Polygon)
            self._df['polygon'] = self._df['polygon'].map(resolve_selfintersection)
        return self._df

    @property
    def df_tissue(self):
        return self.df[self._df.name=='tissue']

    @classmethod
    def resolve_multipolygons(cls, df):
        empty = df.polygon.map(lambda x: x.area ==0)
        df = df[~empty]
        if len(df)==0:
            return df
        mask_multipolygon = df.polygon.map(lambda mpg: isinstance(mpg, MultiPolygon))
        if mask_multipolygon.sum()>0:
            df_mpg = pd.merge(
                         df[mask_multipolygon].drop('polygon', axis=1),
                         (df[mask_multipolygon][['polygon']]
                              .apply(lambda x: x.apply(pd.Series).stack())
                              .reset_index(level=1, drop=True)),
                         right_index=True, left_index=True)
            df = pd.concat([df[~mask_multipolygon], df_mpg], sort=False)
        def is_multistr(x):
            try:
                return int(isinstance(x, GeometryCollection) or isinstance(x.boundary, MultiLineString))
            except ValueError:
                return -1

        mask_collection = df.polygon.map(lambda x: isinstance(x, GeometryCollection))
        if mask_collection.sum()>0:
            df.loc[mask_collection,'polygon'] = df.loc[mask_collection,'polygon'].map(
                            lambda pg: Polygon(pg[np.argmax([x.area for x in  pg])].boundary)
                                                    )
        mask_multistr = df.polygon.map(lambda x: isinstance(x.boundary, MultiLineString))
        #mask_multistr = df.polygon.map(is_multistr)
        if mask_multistr.sum()>0:
            df.loc[mask_multistr==1,'polygon'] = df.loc[mask_multistr==1,'polygon'].map(
                    lambda pg: Polygon(pg.boundary[np.argmax(map(len, pg.boundary))])
                    )
        return df

    def __getitem__(self, key):
        return self.df.iloc[key]


    @classmethod
    def empty_mask(cls, patch_size, scale=1,):
            return np.zeros([int(np.round(x/scale)) for x in patch_size],
                            dtype='uint8')

    def get_patch_rois(self, xc, yc, patch_size, scale=1,
                       translate=True,
                       refine_tissue=None, patch_img=None,
                       get_mask_for_names = None,
                       cocorle=False,
                       **kwargs):
        """extract rois for a given patch centered at `(xc, yc)`,
        original size `patch_size`, given `scale`.
        By default the patch coordinates are translated
        so that upper left corner is at (0,0). 
        In order to turn translation off, set `translate=False`.
        A MS-COCO compatible RLE can be obtained with setting `cocorle=True`

        Inputs:
            xc         -- x-coordinate of the patch centre
            yc         -- y-coordinate of the patch centre
            patch_size -- original patch size
            scale      -- downscale the patch by a given factor
                         (provide a value >1, no upscaling foreseen)
            translate  -- [default: True] translate the ROIs 
                          so that upper left corner is at (0,0)
            cocorle    -- [default: False] produce a MS-COCO RLE
                          (adds fields for `counts` and `size`)
            get_mask_for_names -- return a binary mask given a function
                          that takes names and returns True for matches
        """

        if cocorle:
            from .cocohacks import convert_contour2cocorle as verts2rle
            from .cocohacks import decode
        if 'target_subsample' in kwargs:
            scale = kwargs.pop('target_subsample')
            warn('deprication warning', DeprecationWarning)
        if isinstance(patch_size, int):
            patch_size = [patch_size]*2
        patch_size = [x for x in patch_size]
        patch = CentredRectangle(xc, yc, *patch_size)
        mask = self.df['polygon'].map(lambda x: patch.intersects(x))
        df = self.df[mask].copy()
        df.loc[:,'polygon'] = df['polygon'].map(lambda x: patch & x)
        df = df[df['polygon'].map(lambda x: isinstance(x, (Polygon, MultiPolygon)))]
        if len(df)==0:
            if get_mask_for_names is not None:
                return self.empty_mask(patch_size, scale)
            else:
                return df
        # refine contours of tissue
        if refine_tissue is not None and patch_img is not None:
            patch_img = np.asarray(patch_img)
            scale_img =  patch_size[0] / patch_img.shape[1]
            color=True
            minlen = 50
            filtersize = max(8, patch_img.shape[0]//16)
            mask = get_threshold_tissue_mask(patch_img,
                    color=color, filtersize=filtersize)
            contours = convert_mask2contour(mask, minlen=minlen)
            if len(contours):
                # put contours back into a dataframe
                tissue_polygons = [Polygon(scale_img*co) for co in contours]
                tissue_polygons = map(lambda co: affinity.translate(co, patch.bounds[0], patch.bounds[1]),
                                      tissue_polygons)

                df_tissue = [{'name':'tissue', 'vertices': np.asarray(p.boundary.coords.xy).T.tolist(),
                            'polygon': p} for p in tissue_polygons]
                df_tissue = pd.DataFrame(df_tissue)
                df_tissue.polygon = df_tissue.polygon.map(resolve_selfintersection)
                # match old ROI ids 
                try:
                    df_tissue_old = df[df['name']=='tissue']
                except Exception as ee:
                    warn('df')
                    warn(str(df))
                    raise ee
                #if 'polygon' not in df_tissue:
                for kk,pp in df_tissue_old.set_index('name')['polygon'].items():
                    try:
                        iou_ = df_tissue.polygon.map(lambda x:pp.intersection(x).area/pp.union(x).area)
                    except:
                        print('df_tissue')
                        print(df_tissue)
                        pp = resolve_selfintersection(pp)
                        iou_ = df_tissue.polygon.map(lambda x:pp.intersection(x).area/pp.union(x).area)

                    iou_ = iou_[iou_>0.5]
                    if len(iou_):
                        idx = iou_.idxmax()
                        df_tissue.loc[idx, 'id'] = kk

                df = df[df.name!='tissue']
                df = pd.concat([df, df_tissue], ignore_index=True, sort=False)

        if translate:
            def shift(x):
                return affinity.translate(x, -patch.bounds[0], -patch.bounds[1])
            df.loc[:,'polygon'] = df['polygon'].map(shift)
        if scale != 1:
            def scale_(x, translate=translate):
                origin = (0,0) if translate else (xc, yc)
                return affinity.scale(x, 1/scale, 1/scale, origin=origin)
            df.loc[:,'polygon'] = df['polygon'].map(scale_)

        df = RoiReader.resolve_multipolygons(df)
        if len(df)==0:
            if get_mask_for_names is not None:
                return self.empty_mask(patch_size, scale)
            else:
                return df

        df['area'] = df.polygon.map(lambda x: x.area)
        df['area_fraction'] = df['area'] / np.prod(patch_size) * scale**2
        df.loc[:,'vertices'] = df['polygon'].map(
            lambda p: np.asarray(p.boundary.coords.xy).T.tolist())
        df.loc[:,'area'] = df['polygon'].map(lambda p: p.area)

        if cocorle:
            if not translate:
                raise NotImplementedError('coco RLE is not supported for un-translated ROIs to avoid memory overflow')
            w, h = [int(np.round(ps/scale)) for ps in patch_size]
            df_rle = (df.vertices
                        .map(lambda verts: (verts2rle(verts, w, h)))
                        .apply(pd.Series)
                     )
            df = pd.concat([df, df_rle], axis=1)

        if get_mask_for_names is not None:
            sel_df = df[df.name.map(get_mask_for_names)]
            if len(sel_df)>0:
                return reduce(np.maximum, sel_df.apply(decode, 1))
            else:
                return self.empty_mask(patch_size, scale)

        return ROIFrame(df)


    def get_patch(self, xc, yc, patch_size, scale=1,
                  magn_base = 4, use_cached=True, **kwargs):
        if 'target_subsample' in kwargs:
            scale = kwargs.pop('target_subsample')
            warn('deprication warning', DeprecationWarning)
        if isinstance(patch_size, int):
            patch_size = [patch_size]*2

        patch = _get_patch_(self.slide, xc, yc,
                            patch_size = patch_size,
                            magn_base = magn_base,
                            scale=scale,
                            use_cached=use_cached)
        return patch    


    def plot_patch(self, xc, yc, patch_size, scale=1,
                   magn_base=4, translate=True,
                   cocorle=False,
                   image=True, use_cached=True,
                   colordict={}, figsize=None,
                   vis_scale=True, lw=2,
                   fig=None, ax=None, alpha=0.1, **kwargs):

        if 'target_subsample' in kwargs:
            scale = kwargs.pop('target_subsample')
            warn('deprication warning', DeprecationWarning)
        if isinstance(patch_size, int) or isinstance(patch_size, float):
            patch_size = [int(patch_size)]*2

        if image:
            patch = self.get_patch(xc, yc, patch_size, scale=scale, 
                                   magn_base = magn_base, use_cached=use_cached)

        prois = self.get_patch_rois(xc, yc, patch_size,
                                    refine_tissue=True if image else False,
                                    patch_img=patch if image else None,
                                    scale=scale,
                                    translate=translate,
                                    cocorle=cocorle)
        if fig is None:
            if ax is not None:
                fig = ax.get_figure()
            else:
                if len(kwargs)>0 or figsize is not None:
                    fig, ax = plt.subplots(1, figsize=figsize, **kwargs)
                else:
                    fig = plt.gcf()
                    ax  = fig.gca()
        elif ax is None:
            ax = fig.gca()

        ccycle = plt.rcParams['axes.prop_cycle'].by_key()['color']
        ccycle = cycle(ccycle)

        out_patch_size = patch_size
        if scale != 1:
            if vis_scale:
                out_patch_size = [int(np.round(ps/scale)) for ps in patch_size]
                scale_ = lambda x: x
            else:
                def scale_(x):
                    origin = (0,0) if translate else (xc, yc)
                    return affinity.scale(x, scale, scale, origin=origin)

        if image:
            if translate:
                extent = (0, out_patch_size[0], out_patch_size[1], 0)
            else:
                extent = (xc - out_patch_size[0]//2,
                          xc + out_patch_size[0]//2,
                          yc + out_patch_size[1]//2,
                          yc - out_patch_size[1]//2,
                          )
            ax.imshow(patch,
                      extent=extent)

        if len(prois):
            for name_, gg in prois.groupby('name'):
                flag = True
                if name_ in colordict:
                    c = colordict[name_]
                elif not len(colordict):
                    c = next(ccycle)
                else:
                    continue
                fc = list(colors.to_rgba(c))
                ec = list(colors.to_rgba(c))
                fc[-1] = alpha
                for _, pp_ in gg.iterrows():
                #print(pp_['name'])
                    pp = pp_.polygon
                    if scale !=1:
                        pp = scale_(pp)
                    ax.add_patch(PolygonPatch(pp, fc=fc, ec=ec, lw=lw,
                                 label=name_ if flag else None))
                    flag = False
        ax.relim()
        ax.autoscale_view()
        return fig, ax, patch if image else None, prois


    def plot(self, fig=None, ax=None, labels=True, 
             colors = {}, image=True, annotations=True,
             styles = {},
             **kwargs):
        left = 0
        top = 0
        if not hasattr(self, 'width'):
            self.load_thumbnail()
        right, bottom = self.width, self.height
        if fig is None:
            if ax is not None:
                fig = ax.get_figure()
            else:
                fig, ax = plt.subplots(1, **kwargs)
        elif ax is None:
            ax = fig.gca()

        if image:
            if not hasattr(self, 'img'):
                self.load_thumbnail()
            ax.imshow(self.img, extent=(left, right, bottom, top))

        if annotations:
            ccycle = plt.rcParams['axes.prop_cycle'].by_key()['color']
            last_color = ccycle[-1]
            ccycle = cycle(ccycle[:-1])
            group_columns = ['name']
            if 'color' in self.df.columns:
                group_columns.append('color')

            for group_keys,vv in self.df.groupby(group_columns):
                if 'color' in self.df.columns:
                    kk, cc = group_keys
                else:
                    kk = group_keys

                if kk == 'tissue':
                    if len(styles):
                        if kk in styles:
                            st = styles[kk]
                        else:
                            continue
                    else:
                        st = {}
                    
                    if 'color' in vv.columns:
                        st['c'] = cc
                    elif len(colors):
                        if kk in colors:
                            st['c'] = colors[kk]
                        else:
                            continue
                    else:
                        st['c'] = [0.25]*3

                    start = True
                    for kr, roi in vv.iterrows():
                        label = '{} #{}'.format(kk, roi['id'])
                        vert = roi['vertices']
                        centroid = (sum((x[0] for x in vert)) / len(vert),
                                    sum((x[1] for x in vert)) / len(vert))
                        plot_contour(vert, label=kk if start else None, ax=ax, **st)
                        if labels:
                            ax.text(*centroid, label, color=last_color)
                        start = False 
                else:
                    if len(styles):
                        if kk in styles:
                            st = styles[kk]
                        else:
                            continue
                    elif len(colors):
                        st = {}
                        if kk in colors:
                            st['c'] = colors[kk]
                        else:
                            continue
                    else:
                        cc = next(ccycle)
                        st = {'c': cc}
                    start = True
                    for vert in vv['vertices']:
                        plot_contour(vert, label=kk if start else None, ax=ax, **st)
                        start = False 
                
        return fig, ax

    '''
    def _repr_png_(self):
        """ iPython display hook support
        :returns: png version of the image as bytes
        """
        from io import BytesIO
        #from PIL import Image
        b = BytesIO()
        #Image.fromarray(self.img).save(b, 'png')
        fig, _ = self.plot()
        fig.savefig(b, format='png')
        return b.getvalue()
    '''

    def save(self, outdir=None, keeplevels=1):
        fnjson = self.filenamebase + ".json"
        self.json_filename = fnjson

        if outdir is not None and os.path.isdir(outdir):
            fnjson = fnjson.split('/')[-keeplevels]
            fnjson = os.path.join(outdir, fnjson)
            os.makedirs(os.path.dirname(fnjson), exist_ok = True)

        ## Save both contour lists together
        with open(fnjson, 'w+') as fh:
            json.dump(self.rois, fh)
        return fnjson

    def __repr__(self):
        res = """{} ROIs\n\tfrom{};
        """.format(len(self), self.filenamebase + '.svs')
        return res

    def _repr_html_(self):
        roi_name_counts = pd.Series([rr["name"] for rr in self.rois]).value_counts()
        roi_name_counts.name = 'counts'
        roi_name_counts = roi_name_counts.to_frame()

        prefix = '<h3>{} ROIs\n</h3><p>\tfrom <pre>{}</pre>\n</p>'.format(len(self), self.filenamebase + '.svs')
        return prefix + roi_name_counts._repr_html_()


    def __len__(self):
        return len(self.rois)


class PatchIterator():
    def __init__(self, roireader, vertices=None,  
                 points=None, side=128,
                 subsample=8, batch_size=4, preprocess=lambda x:x,
                 color_last=True,
                 oversample=1, mode='grid',
                 roi = False,
                 get_mask_for_names = None,
                 use_cached=True,
                 verbose=False):

        self.verbose = verbose
        self.use_cached = use_cached
        self.roi = roi
        self.get_mask_for_names = get_mask_for_names
        self.color_last = color_last
        self.roireader = roireader
        self.side_magn = side*subsample
        if points is None and vertices is None:
            raise ValueError('either `points` or `vertices` argument must be provided')

        if points is None:
            self.spacing = self.side_magn/oversample
            self.points = sample_points(vertices, spacing=self.spacing, mode=mode)
        else:
            self.points = points

        self.batch_size = batch_size
        self._batch_size = 1 if batch_size is None or batch_size==0 else batch_size
        self.subsample = subsample
        self.index = -1
        self.indices = np.arange(len(self.points))
        self.preprocess = preprocess

    def __len__(self):
        return int(np.ceil(len(self.points)/self._batch_size))
        
    def __getitem__(self, key):
        start = key*self._batch_size
        end = min(len(self.indices), (1+key)*self._batch_size)
        assert end>start
        batch_x = []
        coords = []
        if self.roi:
            batch_roi = []

        patch_size = [self.side_magn]*2
        for ind in range(start, end):
            pp = self.points[self.indices[ind]]
            if self.verbose:
                print('{}, {}, ({}, {}), target_subsample={}, use_cached={}'
                      .format(*pp,  *[self.side_magn]*2, self.subsample,
                              self.use_cached))

            patch = self.roireader.get_patch(*pp, patch_size,
                                             target_subsample=self.subsample,
                                             use_cached=self.use_cached)
            patch = np.asarray(patch)[...,:3]
            if self.roi:
                try:
                    roi_ = self.roireader.get_patch_rois(*pp, patch_size,
                               scale=self.subsample,
                               translate=True, cocorle=True,
                               refine_tissue=True, patch_img=patch,
                               get_mask_for_names=self.get_mask_for_names,
                               )
                except Exception as ee:
                    warn('error while processing:\n{}, x={:d}, y={:d}'.format(
                         self.roireader.filenamebase, int(pp[0]), int(pp[1])))
                    raise ee

                if not self.color_last and len(roi_.shape)>2:
                    roi_ = roi_.transpose(2,0,1)
                batch_roi.append(roi_)

            patch = self.preprocess(patch)
            if not self.color_last:
                patch = patch.transpose(2,0,1)
            batch_x.append(patch)
            coords.append(pp)

        if self.batch_size is None or self.batch_size==0:
            batch_x = batch_x[0]
            coords = coords[0]
            if self.roi:
                batch_roi = batch_roi[0]
        else:
            batch_x = np.stack(batch_x)
            coords = np.stack(coords)
            if self.roi:
                if (self.get_mask_for_names is not None) and \
                        len(set([x.shape for x in batch_roi]))==1:
                    batch_roi = np.stack(batch_roi)

        if self.roi:
            output = (batch_x, batch_roi, coords)
        else:
            output = (batch_x, coords)
        return output
        
    def __iter__(self):
        return self
    
    def __next__(self):
        self.index += 1

        if self.index >= len(self):
            raise StopIteration

        return self[self.index]
