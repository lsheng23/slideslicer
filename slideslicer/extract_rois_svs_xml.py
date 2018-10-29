
# coding: utf-8
import os
import sys
import re
import json
import openslide
import numpy as np
import pandas as pd
from collections import Counter
from bs4 import BeautifulSoup
#import cv2
import numpy as np
from shapely.geometry import Polygon
from parse_leica_xml import parse_xml2annotations

from slideutils import (get_vertices, get_roi_dict, get_median_color,
                        get_chunk_masks, get_contours_from_mask,
                        get_thumbnail_magnification)

## Read XML ROI, convert, and save as JSON

def _shapely_polygon_from_roi_(roi):
    return Polygon(roi["vertices"])


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
        if roi["name"]=="tissue":
            pgs_tissue[roi['id']] = Polygon(roi["vertices"])
        else:
            pgs_feature[roi['id']] = Polygon(roi["vertices"])

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
    return [roi for roi in roilist if roi['id'] not in empty_chunks]


class RoiReader():
    """ROI reader for Leica SVS slides
    
    """
    def __init__(self, fnxml, threshold_tissue=True, remove_empty=True,
                  save=True, outdir=None, minlen=50,
                  verbose=True):
        """
        extract and save rois

        Inputs:
        fnxml         -- xml path
        remove_empty  -- remove empty chunks of tissue
        outdir        -- (optional); save into an alternative directory
        minlen        -- minimal length of tissue chunk contour in thumbnail image
        keeplevels    -- number of path elements to keep 
                      when saving to provided `outdir`
                      (1 -- filename only; 2 -- incl 1 directory)
        """
        self.filenamebase = re.sub('.svs$','', re.sub(".xml$", "", fnxml))
        self.verbose = verbose
        ############################
        # parsing XML
        ############################
        self.rois = parse_xml2annotations(fnxml)
        for roi in self.rois:
            roi["name"] = roi.pop("text").lower().rstrip('.')
        # for an ellipse, 
        #    area = $\pi \times r \times R$

        if threshold_tissue:
            self.add_tissue(remove_empty=remove_empty,
                   color=False, filtersize=7, minlen=minlen)

        if save:
            self.save()


    def load_thumbnail(self):
        slide = self.slide
        self.img = np.asarray(slide.associated_images["thumbnail"])
        self.width, self.height = slide.dimensions
        self.median_color = get_median_color(slide)
        self._thumbnail_ratio = get_thumbnail_magnification(slide)
        return self.img


    @property
    def slide(self):
        fnsvs = self.filenamebase + ".svs"
        slide_ = openslide.OpenSlide(fnsvs)
        return slide_


    def extract_tissue(self, color=False, filtersize=7, minlen=50):
        ## Extract tissue chunk ROIs
        self.load_thumbnail()

        ## Extract mask and contours
        mask = get_chunk_masks(self.img, color=color, filtersize=filtersize)
        contours = get_contours_from_mask(mask, minlen=minlen)


        sq_micron_per_pixel = np.median([roi["areamicrons"] / roi["area"] for roi in self.rois])

        self.tissue_rois = [get_roi_dict(cc*self._thumbnail_ratio, name='tissue', id=1+nn+len(self.rois),
                                         sq_micron_per_pixel=sq_micron_per_pixel) 
                            for nn,cc in enumerate(contours)]
        return self.tissue_rois 


    def add_tissue(self, remove_empty=True,
                   color=False, filtersize=7, minlen=50):
                   
        if not hasattr(self, 'tissue_rois'):
            self.extract_tissue(color=color, filtersize=filtersize, minlen=minlen) 

        self.rois = self.rois + self.tissue_rois

        if self.verbose:
            print("counts of roi names")
            roi_name_counts = pd.Series([rr["name"] for rr in self.rois]).value_counts()
            print(roi_name_counts)
        
        if remove_empty:
            self.rois = remove_empty_tissue_chunks(self.rois)

            if self.verbose:
                print("counts of roi names after removing empty chunks")
                roi_name_counts = pd.Series([rr["name"] for rr in self.rois]).value_counts()
                print(roi_name_counts)

    @property
    def df(self):
        return pd.DataFrame(self.rois)


    def plot(self, fig=None, ax=None, labels=True):
        import matplotlib.pyplot as plt
        from itertools import cycle
        from slideutils import plot_contour
        if not hasattr(self, 'image'):
            self.load_thumbnail()
        left = 0
        top = 0
        right, bottom = self.width, self.height
        if fig is None:
            if ax is not None:
                fig = ax.get_figure()
            else:
                fig, ax = plt.subplots(1)
        elif ax is None:
            ax = fig.gca()

        ax.imshow(self.img, extent=(left, right, bottom, top))

        ccycle = plt.rcParams['axes.prop_cycle'].by_key()['color']
        last_color = ccycle[-1]
        ccycle = cycle(ccycle[:-1])
        for kk,vv in self.df.groupby('name'):
            if kk == 'tissue':
                cc = [0.25]*3
                start = True
                for kr, roi in vv.iterrows():
                    label = '{} #{}'.format(kk, roi['id'])
                    vert = roi['vertices']
                    centroid = (sum((x[0] for x in vert)) / len(vert), sum((x[1] for x in vert)) / len(vert))
                    plot_contour(vert, label=kk if start else None, c=cc, ax=ax)
                    if labels:
                        ax.text(*centroid, label, color=last_color)
                    start = False 
            else:
                cc = next(ccycle)
                start = True
                for vert in vv['vertices']:
                    plot_contour(vert, label=kk if start else None, c=cc, ax=ax)
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

        prefix = '<h2>{} ROIs\n</h2><p>\tfrom <pre>{}</pre>\n</p>'.format(len(self), self.filenamebase + '.svs')
        return prefix + roi_name_counts._repr_html_()


    def __len__(self):
        return len(self.rois)


def extract_rois_svs_xml(fnxml, remove_empty=True, outdir=None, minlen=50, keeplevels=1):
    """
    extract and save rois

    Inputs:
    fnxml         -- xml path
    remove_empty  -- remove empty chunks of tissue
    outdir        -- (optional); save into an alternative directory
    minlen        -- minimal length of tissue chunk contour in thumbnail image
    keeplevels    -- number of path elements to keep 
                  when saving to provided `outdir`
                  (1 -- filename only; 2 -- incl 1 directory)
    """
    fnsvs = re.sub("\.xml$", ".svs", fnxml)
    fnjson = re.sub(".xml$", ".json", fnxml)
    if outdir is not None and os.path.isdir(outdir):
        fnjson = fnjson.split('/')[-keeplevels]
        fnjson = os.path.join(outdir, fnjson)
        os.makedirs(os.path.dirname(fnjson), exist_ok = True)


    ############################
    # parsing XML
    ############################
    roilist = parse_xml2annotations(fnxml)
    for roi in roilist:
        roi["name"] = roi.pop("text").lower().rstrip('.')
    #import ipdb; ipdb.set_trace()

    #with open(fnxml) as fh:
    #    soup = BeautifulSoup(fh, 'lxml')
    #regions = soup.find_all("region")

    ## fine-parse and format the extracted rois:
    #roilist = []
    #for rr in regions:
    ##     name = rr.get("text").lower().rstrip('.')
    #    attrs_ = rr.attrs.copy()
    #    if ("text" in attrs_) and not ("name" in attrs_):
    #        attrs_["name"] = attrs_.pop("text").lower().rstrip('.')
    #    for kk,vv in attrs_.items():
    #        if isinstance(vv,str) and vv.isdigit():
    #            attrs_[kk] = int(vv)
    #        else:
    #            try:
    #                attrs_[kk] = float(vv)
    #            except:
    #                if attrs_[kk]=='':
    #                    attrs_[kk]=None
    #                continue
    #    attrs_["vertices"] = get_vertices(rr)
    #    roilist.append(attrs_)

    ############################
    ############################

    # for an ellipse, 
    #    area = $\pi \times r \times R$


    #with open(fnjson, 'w+') as fh:
    #    json.dump(roilist, fh)


    ## Extract tissue chunk ROIs

    slide = openslide.OpenSlide(fnsvs)
    img = np.asarray(slide.associated_images["thumbnail"])

    median_color = get_median_color(slide)

    ## Extract mask and contours
    mask = get_chunk_masks(img, color=False, filtersize=7)
    contours = get_contours_from_mask(mask, minlen = minlen)

    ratio = get_thumbnail_magnification(slide)

    sq_micron_per_pixel = np.median([roi["areamicrons"] / roi["area"] for roi in roilist])

    tissue_roilist = [get_roi_dict(cc*ratio, name='tissue', id=1+nn+len(roilist), sq_micron_per_pixel=sq_micron_per_pixel) 
                          for nn,cc in enumerate(contours)]

    roilist = roilist + tissue_roilist

    roi_name_counts = pd.Series([rr["name"] for rr in roilist]).value_counts()
    print("counts of roi names")
    print(roi_name_counts)

    if remove_empty:
        roilist = remove_empty_tissue_chunks(roilist)
        roi_name_counts = pd.Series([rr["name"] for rr in roilist]).value_counts()
        print("counts of roi names after removing empty chunks")
        print(roi_name_counts)
    ## Save both contour lists together
    with open(fnjson, 'w+') as fh:
        json.dump(roilist, fh)

    return fnjson

if __name__ == "__main__":
    fnxml = sys.argv[1]

    outfile = extract_rois_svs_xml(fnxml)
    print(outfile)
