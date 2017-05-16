# TmLibrary - TissueMAPS library for distibuted image analysis routines.
# Copyright (C) 2016  Markus D. Herrmann, University of Zurich and Robin Hafen
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import os
import re
import sys
import shutil
import logging
import subprocess
import numpy as np
import pandas as pd
import collections
import shapely.geometry
import shapely.ops
from cached_property import cached_property
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import FLOAT
from psycopg2 import ProgrammingError
from psycopg2.extras import Json
from gc3libs.quantity import Duration, Memory

import tmlib.models as tm
from tmlib.utils import autocreate_directory_property
from tmlib.utils import flatten
from tmlib.readers import TextReader
from tmlib.readers import ImageReader
from tmlib.writers import TextWriter
from tmlib.models.types import ST_GeomFromText
from tmlib.workflow.api import WorkflowStepAPI
from tmlib.errors import PipelineDescriptionError
from tmlib.errors import JobDescriptionError
from tmlib.workflow.jterator.project import Project
from tmlib.workflow.jterator.module import ImageAnalysisModule
from tmlib.workflow.jterator.handles import SegmentedObjects
from tmlib.workflow.jobs import SingleRunPhase
from tmlib.workflow.jterator.jobs import DebugRunJob
from tmlib.workflow import register_step_api
from tmlib import cfg

logger = logging.getLogger(__name__)


@register_step_api('jterator')
class ImageAnalysisPipelineEngine(WorkflowStepAPI):

    '''Class for running image analysis pipelines.'''

    def __init__(self, experiment_id, pipeline_description=None,
            handles_descriptions=None):
        '''
        Parameters
        ----------
        experiment_id: int
            ID of the processed experiment
        pipeline_description: tmlib.workflow.jterator.description.PipelineDescription, optional
            description of pipeline, i.e. module order and paths to module
            source code and descriptor files (default: ``None``)
        handles_descriptions: Dict[str, tmlib.workflow.jterator.description.HandleDescriptions], optional
            description of module input/output (default: ``None``)

        Note
        ----
        If `pipe` or `handles` are not provided
        they are obtained from the persisted YAML descriptor files on disk.
        '''
        super(ImageAnalysisPipelineEngine, self).__init__(experiment_id)
        self._engines = {'Python': None, 'R': None}
        self.project = Project(
            location=self.step_location,
            pipeline_description=pipeline_description,
            handles_descriptions=handles_descriptions
        )

    @autocreate_directory_property
    def figures_location(self):
        '''str: location where figure files are stored'''
        return os.path.join(self.step_location, 'figures')

    def remove_previous_pipeline_output(self):
        '''Removes all figure files.'''
        shutil.rmtree(self.figures_location)
        os.mkdir(self.figures_location)

    @cached_property
    def pipeline(self):
        '''List[tmlib.jterator.module.ImageAnalysisModule]:
        pipeline built based on
        :class`PipelineDescription <tmlib.workflow.jterator.description.PipelineDescription>` and
        :class:`HandleDescriptions <tmlib.workflow.jterator.description.HandleDescriptions>`.
        '''
        pipeline = list()
        for i, element in enumerate(self.project.pipe.description.pipeline):
            if not element.active:
                continue
            source_file = element.source
            if '/' in source_file:
                source_file = os.path.expandvars(source_file)
                source_file = os.path.expanduser(source_file)
                if not os.path.isabs(source_file):
                    source_file = os.path.join(self.step_location, source_file)
                if not os.path.exists(source_file):
                    raise PipelineDescriptionError(
                        'Module file does not exist: %s' % source_file
                    )
            name = self.project.handles[i].name
            handles = self.project.handles[i].description
            module = ImageAnalysisModule(
                name=name, source_file=source_file, handles=handles
            )
            pipeline.append(module)
        return pipeline

    def start_engines(self):
        '''Starts engines required by non-Python modules in the pipeline.
        This should be done only once, since engines may have long startup
        times, which would otherwise slow down the execution of the pipeline.

        Note
        ----
        For Matlab, you need to set the MATLABPATH environment variable
        in order to add module dependencies to the Matlab path.

        Warning
        -------
        Matlab will be started with the ``"-nojvm"`` option.
        '''
        # TODO: JVM for java code
        languages = [m.language for m in self.pipeline]
        if 'Matlab' in languages:
            logger.info('start Matlab engine')
            try:
                import matlab_wrapper as matlab
            except ImportError:
                raise ImportError(
                    'Matlab engine cannot be started, because '
                    '"matlab-wrapper" package is not installed.'
                )
            # NOTE: It is absolutely necessary to specify these startup options
            # for use parallel processing on the cluster. Otherwise some jobs
            # hang up and get killed due to timeout.
            startup_ops = '-nosplash -singleCompThread -nojvm -nosoftwareopengl'
            logger.debug('Matlab startup options: %s', startup_ops)
            self._engines['Matlab'] = matlab.MatlabSession(options=startup_ops)
            # We have to make sure that code which may be called by a module,
            # are actually on the MATLAB path.
            # To this end, the MATLABPATH environment variable can be used.
            # However, this only adds the folder specified
            # by the environment variable, but not its subfolders. To enable
            # this, we add each directory specified in the environment variable
            # to the path.
            matlab_path = os.environ['MATLABPATH']
            matlab_path = matlab_path.split(':')
            for p in matlab_path:
                if not p:
                    continue
                logger.debug('add "%s" to MATLABPATH', p)
                self._engines['Matlab'].eval(
                    'addpath(genpath(\'{0}\'));'.format(p)
                )
        # if 'Julia' in languages:
        #     print 'jt - Starting Julia engine'
        #     self._engines['Julia'] = julia.Julia()

    def create_run_batches(self, args):
        '''Creates job descriptions for parallel computing.

        Parameters
        ----------
        args: tmlib.workflow.jterator.args.BatchArguments
            step-specific arguments

        Returns
        -------
        generator
            job descriptions
        '''
        channel_names = [
            ch.name for ch in self.project.pipe.description.input.channels
        ]

        if args.plot and args.batch_size != 1:
            raise JobDescriptionError(
                'Batch size must be 1 when plotting is active.'
            )

        with tm.utils.ExperimentSession(self.experiment_id) as session:
            sites = session.query(tm.Site.id).order_by(tm.Site.id).all()
            site_ids = [s.id for s in sites]
            batches = self._create_batches(site_ids, args.batch_size)
            for j, batch in enumerate(batches):
                image_file_locations = session.query(
                        tm.ChannelImageFile._location
                    ).\
                    join(tm.Channel).\
                    filter(tm.Channel.name.in_(channel_names)).\
                    filter(tm.ChannelImageFile.site_id.in_(batch)).\
                    all()
                yield {
                    'id': j + 1,  # job IDs are one-based!
                    'site_ids': batch,
                    'plot': args.plot
                }

    def delete_previous_job_output(self):
        '''Deletes all instances of
        :class:`MapobjectType <tmlib.models.mapobject.MapobjectType>`
        that were generated by a prior run of the same pipeline as well as all
        children instances for the processed experiment.
        '''
        logger.info('delete existing mapobjects and mapobject types')
        with tm.utils.ExperimentConnection(self.experiment_id) as connection:
            tm.MapobjectType.delete_cascade(connection, ref_type='NULL')


    def _load_pipeline_input(self, site_id):
        logger.info('load pipeline inputs')
        # Use an in-memory store for pipeline data and only insert outputs
        # into the database once the whole pipeline has completed successfully.
        store = {
            'site_id': site_id,
            'pipe': dict(),
            'current_figure': list(),
            'objects': dict(),
            'channels': list()
        }

        # Load the images, correct them if requested and align them if required.
        # NOTE: When the experiment was acquired in "multiplexing" mode,
        # images will be automatically aligned, assuming that this is the
        # desired behavior.
        channel_input = self.project.pipe.description.input.channels
        objects_input = self.project.pipe.description.input.objects
        with tm.utils.ExperimentSession(self.experiment_id) as session:
            site = session.query(tm.Site).get(site_id)

            records = session.query(tm.ChannelImageFile.tpoint).\
                filter_by(site_id=site.id).\
                distinct()
            tpoints = [r.tpoint for r in records]
            n_tpoints = len(tpoints)
            records = session.query(tm.ChannelImageFile.zplane).\
                filter_by(site_id=site.id).\
                distinct()
            zplanes = [r.zplane for r in records]
            n_zplanes = len(zplanes)

            y_offset, x_offset = site.aligned_offset
            height = site.aligned_height
            width = site.aligned_width

            for ch in channel_input:
                channel = session.query(
                        tm.Channel.bit_depth, tm.Channel.id
                    ).\
                    filter_by(name=ch.name).\
                    one()
                if channel.bit_depth == 16:
                    dtype = np.uint16
                elif channel.bit_depth == 8:
                    dtype = np.uint8
                image_array = np.zeros(
                    (height, width, n_zplanes, n_tpoints), dtype
                )
                if ch.correct:
                    logger.info(
                        'load illumination statistics for channel "%s"', ch.name
                    )
                    try:
                        stats_file = session.query(tm.IllumstatsFile).\
                            join(tm.Channel).\
                            filter(tm.Channel.name == ch.name).\
                            one()
                    except NoResultFound:
                        raise PipelineDescriptionError(
                            'No illumination statistics file found for '
                            'channel "%s"' % ch.name
                        )
                    stats = stats_file.get()
                else:
                    stats = None

                logger.info('load images for channel "%s"', ch.name)
                image_files = session.query(tm.ChannelImageFile).\
                    filter_by(site_id=site.id, channel_id=channel.id).\
                    all()
                for f in image_files:
                    logger.info('load image %d', f.id)
                    img = f.get()
                    if ch.correct:
                        logger.info('correct image %d', f.id)
                        img = img.correct(stats)
                    logger.debug('align image %d', f.id)
                    img = img.align()  # shifted and cropped!
                    image_array[:, :, f.zplane, f.tpoint] = img.array
                store['pipe'][ch.name] = image_array

            for obj in objects_input:
                mapobject_type = session.query(tm.MapobjectType).\
                    filter_by(name=obj.name).\
                    one()
                polygons = list()
                for t in sorted(tpoints):
                    zpolys = list()
                    for z in sorted(zplanes):
                        zpolys.append(
                            mapobject_type.get_segmentations_per_site(
                                site_id=site.id, tpoint=t, zplane=z
                            )
                        )
                    polygons.append(zpolys)

                segm_obj = SegmentedObjects(obj.name, obj.name)
                segm_obj.add_polygons(
                    polygons, y_offset, x_offset, (height, width)
                )
                store['objects'][segm_obj.name] = segm_obj
                store['pipe'][segm_obj.name] = segm_obj.value

        # Remove single-dimensions from image arrays.
        # NOTE: It would be more consistent to preserve shape, but most people
        # will work with 2D/3D images and having to deal with additional
        # dimensions would be rather annoying I assume.
        for name, img in store['pipe'].iteritems():
            store['pipe'][name] = np.squeeze(img)

        return store

    def _run_pipeline(self, store, site_id, plot=False):
        logger.info('run pipeline')
        for i, module in enumerate(self.pipeline):
            logger.info('run module "%s"', module.name)
            # When plotting is not deriberately activated it defaults to
            # headless mode
            module.update_handles(store, headless=not plot)
            module.run(self._engines[module.language])
            store = module.update_store(store)

            plotting_active = [
                h.value for h in module.handles.input if h.name == 'plot'
            ]
            if len(plotting_active) > 0:
                plotting_active = plotting_active[0]
            else:
                plotting_active = False
            if plot and plotting_active:
                figure_file = module.build_figure_filename(
                    self.figures_location, site_id
                )
                with TextWriter(figure_file) as f:
                    f.write(store['current_figure'])

        return store

    def _build_debug_run_command(self, site_id, verbosity):
        logger.debug('build "debug" command')
        command = [self.step_name]
        command.extend(['-v' for x in range(verbosity)])
        command.append(self.experiment_id)
        command.extend(['debug', '--site', str(site_id), '--plot'])
        return command

    def _save_pipeline_outputs(self, store):
        logger.info('save pipeline outputs')
        objects_output = self.project.pipe.description.output.objects
        for item in objects_output:
            as_polygons = item.as_polygons
            store['objects'][item.name].save = True
            store['objects'][item.name].represent_as_polygons = as_polygons

        with tm.utils.ExperimentSession(self.experiment_id) as session:
            layer = session.query(tm.ChannelLayer).first()
            mapobject_type_ids = dict()
            segmentation_layer_ids = dict()
            objects_to_save = dict()
            feature_ids = collections.defaultdict(dict)
            for obj_name, segm_objs in store['objects'].iteritems():
                if segm_objs.save:
                    logger.info('objects of type "%s" are saved', obj_name)
                    objects_to_save[obj_name] = segm_objs
                else:
                    logger.info('objects of type "%s" are not saved', obj_name)
                    continue
                logger.debug('add object type "%s"', obj_name)
                mapobject_type = session.get_or_create(
                    tm.MapobjectType, experiment_id=self.experiment_id,
                    name=obj_name
                )
                mapobject_type_ids[obj_name] = mapobject_type.id
                # Create a feature values entry for each segmented object at
                # each time point.
                logger.info('add features for objects of type "%s"', obj_name)
                measurements = segm_objs.measurements
                for feature_name in measurements[0].columns:
                    logger.debug('add feature "%s"', feature_name)
                    feature = session.get_or_create(
                        tm.Feature, name=feature_name,
                        mapobject_type_id=mapobject_type_ids[obj_name],
                        is_aggregate=False
                    )
                    feature_ids[obj_name][feature_name] = feature.id

                for (t, z), plane in segm_objs.iter_planes():
                    segmentation_layer = session.get_or_create(
                        tm.SegmentationLayer,
                        mapobject_type_id=mapobject_type.id,
                        tpoint=t, zplane=z
                    )

                    segmentation_layer_ids[(obj_name, t, z)] = segmentation_layer.id
                    # We will update this in collect phase, but we need to set
                    # some limits in case the user already starts viewing
                    # Without any contraints the user interface might explode.
                    poly_thresh = layer.maxzoom_level_index - 3
                    segmentation_layer.polygon_threshold = \
                        0 if poly_thresh < 0 else poly_thresh
                    centroid_thresh = segmentation_layer.polygon_threshold - 2
                    segmentation_layer.centroid_threshold = \
                        0 if centroid_thresh < 0 else centroid_thresh

            site = session.query(tm.Site).get(store['site_id'])
            y_offset, x_offset = site.aligned_offset

        mapobject_ids = dict()
        with tm.utils.ExperimentConnection(self.experiment_id) as conn:
            for obj_name, segm_objs in objects_to_save.iteritems():
                # Delete existing mapobjects for this site when they were
                # generated in a previous run of the same pipeline. In case
                # they were passed to the pipeline as inputs don't delete them
                # because this means they were generated by another pipeline.
                inputs = self.project.pipe.description.input.objects
                if obj_name not in inputs:
                    logger.info(
                        'delete segmentations for existing mapobjects of '
                        'type "%s"', obj_name
                    )
                    tm.Mapobject.delete_cascade(
                        conn, mapobject_type_ids[obj_name],
                        ref_type=tm.Site.__name__, ref_id=store['site_id']
                    )

                # Create a mapobject for each segmented object, i.e. each
                # pixel component having a unique label.
                logger.info('add objects of type "%s"', obj_name)
                mapobjects = [
                    tm.Mapobject(mapobject_type_ids[obj_name])
                    for _ in segm_objs.labels
                ]
                logger.debug('insert mapobjects into db table')
                mapobjects = tm.Mapobject.add_multiple(conn, mapobjects)
                mapobject_ids = {
                    label: mapobjects[i].id
                    for i, label in enumerate(segm_objs.labels)
                }

                # Create a polygon and/or point for each segmented object
                # based on the cooridinates of their contours and centroids,
                # respectively.
                logger.info(
                    'add segmentations for objects of type "%s"', obj_name
                )
                mapobject_segmentations = list()
                if segm_objs.represent_as_polygons:
                    logger.debug('represent segmented objects as polygons')
                    iterator = segm_objs.iter_polygons(y_offset, x_offset)
                    for t, z, label, polygon in iterator:
                        logger.debug(
                            'add segmentation for object #%d at '
                            'tpoint %d and zplane %d', label, t, z
                        )
                        if polygon.is_empty:
                            logger.warn(
                                'object #%d of type %s doesn\'t have a polygon',
                                label, obj_name
                            )
                            # TODO: Shall we rather raise an Exception here???
                            # At the moment we remove the corresponding
                            # mapobjects in the collect phase.
                            continue
                        mapobject_segmentations.append(
                            tm.MapobjectSegmentation(
                                geom_polygon=polygon,
                                geom_centroid=polygon.centroid,
                                mapobject_id=mapobject_ids[label],
                                segmentation_layer_id=segmentation_layer_ids[
                                    (obj_name, t, z)
                                ],
                                label=label
                            )
                        )
                else:
                    logger.debug('represent segmented objects only as points')
                    iterator = segm_objs.iter_points(y_offset, x_offset)
                    for t, z, label, centroid in iterator:
                        logger.debug(
                            'add segmentation for object #%d at '
                            'tpoint %d and zplane %d', label, t, z
                        )
                        mapobject_segmentations.append(
                            tm.MapobjectSegmentation(
                                geom_polygon=None,
                                geom_centroid=centroid,
                                mapobject_id=mapobject_ids[label],
                                segmentation_layer_id=segmentation_layer_ids[
                                    (obj_name, t, z)
                                ],
                                label=label
                            )
                        )
                logger.debug('insert mapobject segmentations into db table')
                tm.MapobjectSegmentation.add_multiple(
                    conn, mapobject_segmentations
                )

                # # Create a feature values entry for each segmented object at
                # # each time point.
                # logger.info('add features for objects of type "%s"', obj_name)
                # measurements = segm_objs.measurements
                # feature_ids = dict()
                # for fname in measurements[0].columns:
                #     logger.debug('add feature "%s"', fname)
                #     feature_ids[fname] = self._add_feature(
                #         conn, fname, mapobject_type_ids[obj_name], False
                #     )

                logger.debug('round feature values to 6 decimals')
                feature_values = list()
                for t, data in enumerate(measurements):
                    data = data.round(6)  # single!
                    if data.empty:
                        logger.warn('empty measurement at time point %d', t)
                        continue
                    column_lut = feature_ids[obj_name]
                    for label, d in data.rename(columns=column_lut).iterrows():
                        logger.debug(
                            'add values for mapobject #%d at time point %d',
                            label, t
                        )
                        values = dict(
                            zip(d.index.astype(str), d.values.astype(str))
                        )
                        feature_values.append(
                            tm.FeatureValues(
                                mapobject_id=mapobject_ids[label],
                                tpoint=t,
                                values=values
                            )
                        )
                logger.debug('insert feature values into db table')
                tm.FeatureValues.add_multiple(conn, feature_values)

    def create_debug_run_phase(self, submission_id):
        '''Creates a job collection for the debug "run" phase of the step.

        Parameters
        ----------
        submission_id: int
            ID of the corresponding
            :class:`Submission <tmlib.models.submission.Submission>`

        Returns
        -------
        tmlib.workflow.job.RunPhase
            collection of debug "run" jobs
        '''
        return SingleRunPhase(
            step_name=self.step_name, submission_id=submission_id,
            parent_id=None
        )

    def create_debug_run_jobs(self, user_name, job_collection,
            batches, verbosity, duration, memory, cores):
        '''Creates debug jobs for the parallel "run" phase of the step.

        Parameters
        ----------
        user_name: str
            name of the submitting user
        job_collection: tmlib.workflow.job.RunPhase
            empty collection of *run* jobs that should be populated
        batches: List[dict]
            job descriptions
        verbosity: int
            logging verbosity for jobs
        duration: str
            computational time that should be allocated for a single job;
            in HH:MM:SS format
        memory: int
            amount of memory in Megabyte that should be allocated for a single
        cores: int
            number of CPU cores that should be allocated for a single job

        Returns
        -------
        tmlib.workflow.jobs.RunPhase
            run jobs
        '''
        logger.info(
            'create "debug" run jobs for submission %d',
            job_collection.submission_id
        )
        logger.debug('allocated time for debug run jobs: %s', duration)
        logger.debug('allocated memory for debug run jobs: %d MB', memory)
        logger.debug('allocated cores for debug run jobs: %d', cores)

        for b in batches:
            job = DebugRunJob(
                step_name=self.step_name,
                arguments=self._build_debug_run_command(b['site_id'], verbosity),
                output_dir=self.log_location,
                job_id=b['site_id'],
                submission_id=job_collection.submission_id,
                parent_id=job_collection.persistent_id,
                user_name=user_name
            )
            job.requested_walltime = Duration(duration)
            job.requested_memory = Memory(memory, Memory.MB)
            if not isinstance(cores, int):
                raise TypeError(
                    'Argument "cores" must have type int.'
                )
            if not cores > 0:
                raise ValueError(
                    'The value of "cores" must be positive.'
                )
            job.requested_cores = cores
            job_collection.add(job)
        return job_collection

    def run_job(self, batch):
        '''Runs the pipeline, i.e. executes modules sequentially. After
        successful completion of the pipeline, instances of
        :class:`MapobjectType <tmlib.models.mapobject.MapobjectType>`,
        :class:`Mapobject <tmlib.models.mapobject.Mapobject>`,
        :class:`SegmentationLayer <tmlib.models.layer.SegmentationLayer>`,
        :class:`MapobjectSegmentation <tmlib.models.mapobject.MapobjectSegmentation>`,
        :class:`Feature <tmlib.models.feature.Feature>` and
        :class:`FeatureValues <tmlib.models.feature.FeatureValues>`
        are created and persisted in the database for subsequent
        visualization and analysis.

        Parameters
        ----------
        batch: dict
            job description
        '''
        logger.info('handle pipeline input')

        self.start_engines()

        # Enable debugging of pipelines by providing the full path to images.
        # This requires a work around for "plot" and "job_id" arguments.
        for site_id in batch['site_ids']:
            logger.info('process site %d', site_id)
            store = self._load_pipeline_input(site_id)
            store = self._run_pipeline(store, site_id, batch['plot'])
            self._save_pipeline_outputs(store)

    def _aggregate(self, feature_map, mapobject_type_id, mapobject_type_name,
            ref_mapobject_type_id, ref_mapobject_id, ref_geometry):
        with tm.utils.ExperimentConnection(self.experiment_id) as c:
            c.execute('''
                SELECT v.tpoint, array_agg(v.values) AS data
                FROM feature_values AS v
                JOIN mapobjects AS m
                ON m.id = v.mapobject_id
                JOIN mapobject_segmentations AS s
                ON s.mapobject_id = v.mapobject_id
                WHERE m.mapobject_type_id=%(mapobject_type_id)s
                AND ST_Intersects(
                    s.geom_polygon, ST_GeomFromText(%(ref_polygon)s)
                )
                GROUP BY tpoint;
            ''', {
                'mapobject_type_id': mapobject_type_id,
                'ref_polygon': ref_geometry
            })
            results = c.fetchall()

            statistics = {
                'Min': np.nanmin, 'Max': np.nanmax, 'Sum': np.nansum,
                'Mean': np.nanmean, 'Count': len
            }
            agg_values = dict()
            agg_features = dict()
            for record in results:
                t = record.tpoint
                data = pd.DataFrame(record.data).astype(float)
                data.rename(columns=feature_map, inplace=True)
                for stat_name, func in statistics.iteritems():
                    for feature_name, vals in data.iteritems():
                        agg_value = np.round(func(vals.values), 6)
                        if stat_name == 'count':
                            agg_feature_name = '{type}_{stat}'.format(
                                type=mapobject_type_name, stat=stat_name
                            )
                        else:
                            agg_feature_name = '{name}_{type}_{stat}'.format(
                                name=feature_name,
                                type=mapobject_type_name, stat=stat_name
                            )
                        agg_id = self._add_feature(
                            c, agg_feature_name, ref_mapobject_type_id, True
                        )
                        agg_values[str(agg_id)] = str(agg_value)
                        agg_features[str(agg_id)] = agg_feature_name
                feature_values = tm.FeatureValues(ref_mapobject_id, agg_values, t)
                tm.FeatureValues.add(c, feature_values)

            return agg_features

    def _aggregate_aggregates(self, feature_map,
            mapobject_type_id, mapobject_type_name,
            ref_mapobject_type_id, ref_mapobject_id, ref_geometry):
        with tm.utils.ExperimentConnection(self.experiment_id) as c:
            c.execute('''
                SELECT v.tpoint, array_agg(v.values) AS data
                FROM feature_values AS v
                JOIN mapobjects AS m
                ON m.id = v.mapobject_id
                JOIN mapobject_segmentations AS s
                ON s.mapobject_id = v.mapobject_id
                WHERE m.mapobject_type_id=%(mapobject_type_id)s
                AND ST_Intersects(
                    s.geom_polygon, ST_GeomFromText(%(ref_polygon)s)
                )
                GROUP BY tpoint;
            ''', {
                'mapobject_type_id': mapobject_type_id,
                'ref_polygon': ref_geometry
            })
            records = c.fetchall()

            statistics = {
                'Min': np.nanmin, 'Max': np.nanmax, 'Sum': np.nansum,
                'Mean': np.nanmean, 'Count': np.sum
            }
            agg_values = dict()
            agg_features = dict()
            for record in records:
                t = record.tpoint
                data = pd.DataFrame(record.data).astype(float)
                data.rename(columns=feature_map, inplace=True)
                for feature_name, vals in data.iteritems():
                    stat_name = re.search(r'_([^_]+)$', feature_name).group(1)
                    func = statistics[stat_name]
                    agg_value = np.round(func(vals.values), 6)
                    agg_feature_name = feature_name
                    agg_id = self._add_feature(
                        c, agg_feature_name, ref_mapobject_type_id, True
                    )
                    agg_values[str(agg_id)] = str(agg_value)
                    agg_features[str(agg_id)] = agg_feature_name
                feature_values = tm.FeatureValues(ref_mapobject_id, agg_values, t)
                tm.FeatureValues.add(c, feature_values)

            return agg_features

    def collect_job_output(self, batch):
        '''Computes the optimal representation of each
        :class:`SegmentationLayer <tmlib.models.layer.SegmentationLayer>` on the
        map for zoomable visualization.

        Parameters
        ----------
        batch: dict
            job description
        '''
        logger.info('clean-up mapobjects with invalid or missing segmentations')

        with tm.utils.ExperimentSession(self.experiment_id) as session:
            layer = session.query(tm.ChannelLayer).first()
            maxzoom = layer.maxzoom_level_index
            segmentation_layers = session.query(tm.SegmentationLayer).all()
            for segm_layer in segmentation_layers:
                pt, ct = segm_layer.calculate_zoom_thresholds(maxzoom)
                segm_layer.polygon_threshold = pt
                segm_layer.centroid_threshold = ct

        with tm.utils.ExperimentSession(self.experiment_id) as session:
            ref_types = [tm.Plate.__name__, tm.Well.__name__, tm.Site.__name__]
            mapobject_types = session.query(tm.MapobjectType.id).\
                filter(tm.MapobjectType.ref_type.in_(ref_types)).\
                all()
            mapobject_type_ids = [m.id for m in mapobject_types]

        with tm.utils.ExperimentConnection(self.experiment_id) as connection:
            tm.Mapobject.delete_invalid_cascade(connection)
            tm.Mapobject.delete_missing_cascade(connection)
            for mid in mapobject_type_ids:
                tm.Feature.delete_cascade(connection, mapobject_type_id=mid)

        logger.info('compute aggregate features')
        with tm.utils.ExperimentSession(self.experiment_id) as session:
            mapobject_types = session.query(
                    tm.MapobjectType.id, tm.MapobjectType.name
                ).\
                filter(tm.MapobjectType.ref_type == None).\
                all()
            mapobject_type_id_lut = dict()
            feature_lut = dict()
            for mapobject_type_id, mapobject_type_name in mapobject_types:
                mapobject_type_id_lut[mapobject_type_name] = mapobject_type_id
                features = session.query(tm.Feature.id, tm.Feature.name).\
                    filter_by(mapobject_type_id=mapobject_type_id).\
                    all()
                feature_lut[mapobject_type_name] = {
                    str(id): name for id, name in features
                }

        parent_mapobject_ref_types = ['Site', 'Well', 'Plate']
        with tm.utils.ExperimentSession(self.experiment_id) as session:
            ref_id_lut = dict()
            ref_lut = dict()
            for ref_type in parent_mapobject_ref_types:
                ref_mapobject_type = session.query(tm.MapobjectType.id).\
                    filter_by(ref_type=ref_type).\
                    one()
                ref_mapobject_type_id = ref_mapobject_type.id
                ref_mapobjects = session.query(
                        tm.MapobjectSegmentation.mapobject_id,
                        tm.MapobjectSegmentation.geom_polygon.ST_AsText()
                    ).\
                    join(tm.Mapobject).\
                    filter(
                        tm.Mapobject.mapobject_type_id == ref_mapobject_type.id
                    ).\
                    all()
                ref_id_lut[ref_type] = ref_mapobject_type.id
                ref_lut[ref_type] = [(r[0], r[1]) for r in ref_mapobjects]

        # TODO: Load all feature values for each site into memory as DataFrame
        # and aggregate in memory.
        new_features = collections.defaultdict(dict)
        for ref_type_name in parent_mapobject_ref_types:
            ref_type_id = ref_id_lut[ref_type_name]
            logger.info('aggregate feature values per "%s"', ref_type_name)
            for ref_id, ref_geom in ref_lut[ref_type_name]:
                logger.debug('compute aggregates for object #%d', ref_id)
                if ref_type_name == 'Site':
                    for mapobject_type_name, features in feature_lut.iteritems():
                        mapobject_type_id = mapobject_type_id_lut[
                            mapobject_type_name
                        ]
                        logger.debug(
                            'compute aggregates over feature values'
                            'for all child objects of type "%s"',
                            mapobject_type_name
                        )
                        f = self._aggregate(
                            feature_map=features,
                            mapobject_type_id=mapobject_type_id,
                            mapobject_type_name=mapobject_type_name,
                            ref_mapobject_type_id=ref_type_id,
                            ref_mapobject_id=ref_id,
                            ref_geometry=ref_geom
                        )
                        new_features[ref_type_name].update(f)
                else:
                    # Now, we simply aggregate the statistics over the next
                    # "lower" level reference mapobject type:
                    # sites per well and wells per plate.
                    idx = parent_mapobject_ref_types.index(ref_type_name) - 1
                    mapobject_type_name = parent_mapobject_ref_types[idx]
                    mapobject_type_id = ref_id_lut[mapobject_type_name]
                    f = self._aggregate_aggregates(
                        feature_map=new_features[mapobject_type_name],
                        mapobject_type_id=mapobject_type_id,
                        mapobject_type_name=mapobject_type_name,
                        ref_mapobject_type_id=ref_type_id,
                        ref_mapobject_id=ref_id,
                        ref_geometry=ref_geom
                    )
                    new_features[ref_type_name].update(f)

        # TODO: population context
        # tm.types.ST_Expand()

    @staticmethod
    def _add_feature(conn, name, mapobject_type_id, is_aggregate):
        conn.execute('''
            INSERT INTO features (name, is_aggregate, mapobject_type_id)
            VALUES (%(name)s, %(is_aggregate)s, %(mapobject_type_id)s)
            ON CONFLICT
            ON CONSTRAINT features_name_mapobject_type_id_key
            DO NOTHING
            RETURNING id
        ''', {
            'name': name,
            'is_aggregate': is_aggregate,
            'mapobject_type_id': mapobject_type_id
        })
        record = conn.fetchone()
        if record is None:
            conn.execute('''
                SELECT id FROM features
                WHERE name = %(name)s
                AND mapobject_type_id = %(mapobject_type_id)s
            ''', {
                'name': name,
                'mapobject_type_id': mapobject_type_id
            })
            record = conn.fetchone()
        return record.id
