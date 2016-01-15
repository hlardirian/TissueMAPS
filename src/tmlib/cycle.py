import re
import os
import logging
import numpy as np
import pandas as pd
from natsort import natsorted
from cached_property import cached_property
from cached_property import threaded_cached_property
from . import utils
from .readers import JsonReader
from .image import is_image_file
from .image import ChannelImage
from .image import IllumstatsImages
from .metadata import ChannelImageMetadata
from .metadata import IllumstatsImageMetadata
from .errors import RegexError
from align.description import AlignmentDescription

logger = logging.getLogger(__name__)


class Cycle(object):
    '''
    A *cycle* represents an individual image acquisition time point
    as part of a time series experiment and corresponds to a folder on disk.

    The `Cycle` class provides attributes and methods for accessing the
    contents of this folder.

    See also
    --------
    :py:class:`tmlib.experiment.Experiment`
    '''

    CYCLE_DIR_FORMAT = 'cycle_{index:0>2}'

    STATS_FILE_FORMAT = 'channel_{channel_ix}.stat.h5'

    def __init__(self, cycle_dir, library):
        '''
        Initialize an instance of class Cycle.

        Parameters
        ----------
        cycle_dir: str
            absolute path to the cycle directory
        library: str
            image library that should be used
            (options: ``"vips"`` or ``"numpy"``)

        Returns
        -------
        tmlib.cycle.Cycle

        Raises
        ------
        OSError
            when `cycle_dir` does not exist
        '''
        self.cycle_dir = os.path.abspath(cycle_dir)
        if not os.path.exists(self.cycle_dir):
            raise OSError('Cycle directory does not exist.')
        self.library = library

    @property
    def dir(self):
        '''
        Returns
        -------
        str
            absolute path to the cycle folder
        '''
        return self.cycle_dir

    @property
    def index(self):
        '''
        A *cycle* represents a time point in a time series. The `index`
        is the zero-based index of the *cycle* in this sequence.
        It is encoded in the name of the *cycle* folder and is retrieved from
        it using a regular expression.

        Returns
        -------
        int
            zero-based cycle index

        Raises
        ------
        RegexError
            when `index` cannot not be determined from folder name
        '''
        folder_name = os.path.basename(self.dir)
        regexp = utils.regex_from_format_string(self.CYCLE_DIR_FORMAT)
        match = re.search(regexp, folder_name)
        if not match:
            raise RegexError(
                    'Can\'t determine cycle id number from folder "%s" '
                    'using format "%s" provided by the configuration settings.'
                    % (folder_name, self.CYCLE_DIR_FORMAT))
        return int(match.group('index'))

    @property
    def experiment_dir(self):
        '''
        Returns
        -------
        str
            absolute path to the parent experiment directory
        '''
        return os.path.dirname(self.dir)

    @property
    def experiment(self):
        '''
        Returns
        -------
        str
            name of the corresponding parent experiment folder
        '''
        return os.path.basename(os.path.dirname(self.dir))

    @cached_property
    def image_dir(self):
        '''
        Returns
        -------
        str
            path to the folder holding the image files

        Note
        ----
        The directory is created if it doesn't exist.
        '''
        image_dir = os.path.join(self.dir, 'images')
        if not os.path.exists(image_dir):
            logger.debug('create directory for image files: %s', image_dir)
            os.mkdir(image_dir)
        return image_dir

    @cached_property
    def image_files(self):
        '''
        Returns
        -------
        List[str]
            names of files in `image_dir`

        Raises
        ------
        OSError
            when no image files are found in `image_dir`

        See also
        --------
        :py:func:`tmlib.image.is_image_file`
        '''
        files = [
            f for f in os.listdir(self.image_dir) if is_image_file(f)
        ]
        files = natsorted(files)
        if not files:
            raise OSError('No image files found in "%s"' % self.image_dir)
        return files

    @property
    def image_metadata_file(self):
        '''
        Returns
        -------
        str
            name of the HDF5 file containing cycle-specific image metadata
        '''
        return 'image_metadata.h5'

    @property
    def align_descriptor_file(self):
        '''
        Returns
        -------
        str
            name of the file that contains cycle-specific descriptions required
            for the alignment of images of the current cycle relative to the
            reference cycle
        '''
        return 'alignment_description.json'

    @threaded_cached_property
    def image_metadata(self):
        '''
        Returns
        -------
        pandas.DataFrame
            metadata for each file in `image_dir` in tabular form, where
            rows represent images and columns hold the values for different
            metadata attributes

        Raises
        ------
        OSError
            when metadata file does not exist

        Note
        ----
        The table representation of metadata is cached in memory and allows
        efficient indexing. See
        `pandas docs <http://pandas.pydata.org/pandas-docs/stable/indexing.html>`_
        for details on indexing and selecting data.
        '''
        metadata_file = os.path.join(self.dir, self.image_metadata_file)
        logger.debug('read image metadata from HDF5 file')
        store = pd.HDFStore(metadata_file)
        metadata = store.select('metadata').sort_values(by='name')
        metadata.index = range(metadata.shape[0])
        store.close()

        # TODO: consider returning the store for subset selection using the
        # select(), select_column(), or select_as_multiple() methods

        # Add the alignment description to each image element (if available)
        alignment_file = os.path.join(self.dir, self.align_descriptor_file)
        if os.path.exists(alignment_file):
            with JsonReader() as reader:
                description = reader.read(alignment_file)
            align_description = AlignmentDescription(description)
            # Match shift descriptions via "site_ix"
            sites = metadata['site_ix']
            n_sites = len(sites)
            overhang = align_description.overhang
            metadata['upper_overhang'] = np.repeat(overhang.upper, n_sites)
            metadata['lower_overhang'] = np.repeat(overhang.lower, n_sites)
            metadata['right_overhang'] = np.repeat(overhang.right, n_sites)
            metadata['left_overhang'] = np.repeat(overhang.left, n_sites)
            metadata['x_shift'] = np.repeat(0, n_sites)
            metadata['y_shift'] = np.repeat(0, n_sites)
            align_sites = [shift.site_ix for shift in align_description.shifts]
            for i, s in enumerate(sites):
                ix = align_sites.index(s)
                shift = align_description.shifts[ix]
                metadata.at[i, 'x_shift'] = shift.x
                metadata.at[i, 'y_shift'] = shift.y

        # TODO: why is this property not cached???

        return metadata

    @property
    def images(self):
        '''
        Returns
        -------
        List[tmlib.image.ChannelImage]
            image object for each image file in `image_dir`

        Note
        ----
        Image objects have lazy loading functionality, i.e. the actual image
        pixel array is only loaded into memory once the corresponding attribute
        (property) is accessed.

        Raises
        ------
        ValueError
            when names of image files and names in the image metadata are not
            the same
        '''
        # Get all images
        index = xrange(len(self.image_files))
        return self.get_image_subset(index)

    def get_image_subset(self, indices):
        '''
        Create image objects for a subset of image files in `image_dir`.

        Parameters
        ----------
        indices: List[int]
            indices of image files for which an image object should be created

        Returns
        -------
        List[tmlib.image.ChannelImage]
            image objects
        '''
        images = list()
        filenames = self.image_metadata.name
        # if self.image_files != filenames.tolist():
        #     raise ValueError('Names of images do not match')
        for i in indices:
            f = self.image_files[i]
            logger.debug('create image "%s"', f)
            image_metadata = ChannelImageMetadata()
            table = self.image_metadata[(filenames == f)]
            for attr in table:
                value = table.iloc[0][attr]
                setattr(image_metadata, attr, value)
            image_metadata.id = table.index[0]
            img = ChannelImage.create_from_file(
                    filename=os.path.join(self.image_dir, f),
                    metadata=image_metadata,
                    library=self.library)
            images.append(img)
        return images

    @cached_property
    def stats_dir(self):
        '''
        Returns
        -------
        str
            path to the directory holding illumination statistic files

        Note
        ----
        Directory is created if it doesn't exist.
        '''
        stats_dir = os.path.join(self.dir, 'stats')
        if not os.path.exists(stats_dir):
            logger.debug(
                'create directory for illumination statistics files: %s',
                stats_dir)
            os.mkdir(stats_dir)
        return stats_dir

    @cached_property
    def illumstats_files(self):
        '''
        Returns
        -------
        Dict[int, str]
            name of the illumination correction file for each channel

        Note
        ----
        Metadata information is retrieved from the filenames using regular
        expressions.

        Raises
        ------
        OSError
            when no illumination statistic files are found in `stats_dir`
        '''
        stats_pattern = self.STATS_FILE_FORMAT.format(channel_ix='([0-9])')
        regexp = re.compile(stats_pattern)
        files = {
            int(regexp.search(f).group(1)): f
            for f in os.listdir(self.stats_dir) if regexp.search(f)
        }
        if not files:
            raise OSError('No illumination statistic files found in "%s"'
                          % self.stats_dir)
        return files

    @property
    def illumstats_metadata(self):
        '''
        Returns
        -------
        Dict[int, tmlib.image.IllumstatsImageMetadata]
            illumination statistics metadata for each channel
        '''
        illumstats_metadata = dict()
        for c, f in self.illumstats_files.iteritems():
            md = IllumstatsImageMetadata()
            md.channel_ix = c
            md.tpoint_ix = self.index
            md.filename = f
            illumstats_metadata[c] = md
        return illumstats_metadata

    @property
    def illumstats_images(self):
        '''
        Returns
        -------
        Dict[int, tmlib.image.IllumstatsImages]
            illumination statistics images for each channel

        Note
        ----
        Image objects have lazy loading functionality, i.e. the actual image
        pixel array is only loaded into memory once the corresponding attribute
        is accessed.
        '''
        illumstats_images = dict()
        for c, f in self.illumstats_files.iteritems():
            img = IllumstatsImages.create_from_file(
                    filename=os.path.join(self.stats_dir, f),
                    metadata=self.illumstats_metadata[c],
                    library=self.library)
            illumstats_images[c] = img
        return illumstats_images
