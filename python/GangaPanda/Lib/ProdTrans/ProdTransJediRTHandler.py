import commands, exceptions, random, re, sys, time

from Ganga.Core.exceptions import ApplicationConfigurationError
from Ganga.GPIDev.Adapters.IRuntimeHandler import IRuntimeHandler
from Ganga.Core import BackendError

from GangaAtlas.Lib.ATLASDataset.DQ2Dataset import getDatasets

import Ganga.Utility.logging
logger = Ganga.Utility.logging.getLogger()

from Ganga.Utility.Config import getConfig
configPanda = getConfig('Panda')

class ProdTransJediRTHandler(IRuntimeHandler):
    """Runtime handler for the ProdTrans application."""

    def master_prepare(self, app, appmasterconfig):
        """Prepare the master aspect of job submission.
           Returns: jobmasterconfig understood by Panda backend."""

        from pandatools import Client
        from pandatools import AthenaUtils
        from taskbuffer.JobSpec import JobSpec
        from taskbuffer.FileSpec import FileSpec
        from GangaAtlas.Lib.ATLASDataset.DQ2Dataset import dq2_set_dataset_lifetime
        from GangaPanda.Lib.Panda.Panda import refreshPandaSpecs

        job = app._getParent()
        logger.debug('ProdTransJediRTHandler master_prepare() for %s',
                    job.getFQID('.'))

        # make sure we have the correct siteType
        #refreshPandaSpecs()

        job = app._getParent()
        masterjob = job._getRoot()

        job.backend.actualCE = job.backend.site
        job.backend.requirements.cloud = Client.PandaSites[job.backend.site]['cloud']

        # JobSpec.
        jspec = JobSpec()
        jspec.currentPriority = app.priority
        jspec.jobDefinitionID = masterjob.id
        jspec.jobName = commands.getoutput('uuidgen 2> /dev/null')
        jspec.coreCount = app.core_count
        jspec.AtlasRelease = 'Atlas-%s' % app.atlas_release
        jspec.homepackage = app.home_package
        jspec.transformation = app.transformation
        jspec.destinationDBlock = job.outputdata.datasetname
        if job.outputdata.location:
            jspec.destinationSE = job.outputdata.location
        else:
            jspec.destinationSE = job.backend.site
        if job.inputdata:
            jspec.prodDBlock = job.inputdata.dataset[0]
        else:
            jspec.prodDBlock = 'NULL'
        if app.prod_source_label:
            jspec.prodSourceLabel = app.prod_source_label
        else:
            jspec.prodSourceLabel = configPanda['prodSourceLabelRun']
        jspec.processingType = configPanda['processingType']
        jspec.specialHandling = configPanda['specialHandling']
        jspec.computingSite = job.backend.site
        jspec.cloud = job.backend.requirements.cloud
        jspec.cmtConfig = app.atlas_cmtconfig
        if app.dbrelease == 'LATEST':
            try:
                latest_dbrelease = getLatestDBReleaseCaching()
            except:
                from pandatools import Client
                latest_dbrelease = Client.getLatestDBRelease()
            m = re.search('(.*):DBRelease-(.*)\.tar\.gz', latest_dbrelease)
            if m:
                self.dbrelease_dataset = m.group(1)
                self.dbrelease = m.group(2)
            else:
                raise ApplicationConfigurationError(None, "Error retrieving LATEST DBRelease. Try setting application.dbrelease manually.")
        else:
            self.dbrelease_dataset = app.dbrelease_dataset
            self.dbrelease = app.dbrelease
        jspec.jobParameters = app.job_parameters

        if self.dbrelease:
            if self.dbrelease == 'current':
                jspec.jobParameters += ' --DBRelease=current'
            else:
                if jspec.transformation.endswith("_tf.py") or jspec.transformation.endswith("_tf"):
                    jspec.jobParameters += ' --DBRelease=DBRelease-%s.tar.gz' % (self.dbrelease,)
                else:
                    jspec.jobParameters += ' DBRelease=DBRelease-%s.tar.gz' % (self.dbrelease,)
                dbspec = FileSpec()
                dbspec.lfn = 'DBRelease-%s.tar.gz' % self.dbrelease
                dbspec.dataset = self.dbrelease_dataset
                dbspec.prodDBlock = jspec.prodDBlock
                dbspec.type = 'input'
                jspec.addFile(dbspec)

        if job.inputdata:
            m = re.search('(.*)\.(.*)\.(.*)\.(.*)\.(.*)\.(.*)',
                          job.inputdata.dataset[0])
            if not m:
                logger.error("Error retrieving run number from dataset name")
                #raise ApplicationConfigurationError(None, "Error retrieving run number from dataset name")
                runnumber = 105200
            else:
                runnumber = int(m.group(2))
            if jspec.transformation.endswith("_tf.py") or jspec.transformation.endswith("_tf"):
                jspec.jobParameters += ' --runNumber %d' % runnumber
            else:
                jspec.jobParameters += ' RunNumber=%d' % runnumber

        # Output files.
        randomized_lfns = []
        ilfn = 0
        for lfn, lfntype in zip(app.output_files,app.output_type):
            ofspec = FileSpec()
            if app.randomize_lfns:
                randomized_lfn = lfn + ('.%s.%d.%s' % (job.backend.site, int(time.time()), commands.getoutput('uuidgen 2> /dev/null')[:4] ) )
            else:
                randomized_lfn = lfn
            ofspec.lfn = randomized_lfn
            randomized_lfns.append(randomized_lfn)
            ofspec.destinationDBlock = jspec.destinationDBlock
            ofspec.destinationSE = jspec.destinationSE
            ofspec.dataset = jspec.destinationDBlock
            ofspec.type = 'output'
            jspec.addFile(ofspec)
            if jspec.transformation.endswith("_tf.py") or jspec.transformation.endswith("_tf"):
                jspec.jobParameters += ' --output%sFile %s' % (lfntype, randomized_lfns[ilfn])
            else:
                jspec.jobParameters += ' output%sFile=%s' % (lfntype, randomized_lfns[ilfn])
            ilfn=ilfn+1

        # Input files.
        if job.inputdata:
            for guid, lfn, size, checksum, scope in zip(job.inputdata.guids, job.inputdata.names, job.inputdata.sizes, job.inputdata.checksums, job.inputdata.scopes):
                ifspec = FileSpec()
                ifspec.lfn = lfn
                ifspec.GUID = guid
                ifspec.fsize = size
                ifspec.md5sum = checksum
                ifspec.scope = scope
                ifspec.dataset = jspec.prodDBlock
                ifspec.prodDBlock = jspec.prodDBlock
                ifspec.type = 'input'
                jspec.addFile(ifspec)
            if app.input_type:
                itype = app.input_type
            else:
                itype = m.group(5)
            if jspec.transformation.endswith("_tf.py") or jspec.transformation.endswith("_tf"):
                jspec.jobParameters += ' --input%sFile %s' % (itype, ','.join(job.inputdata.names))
            else:
                jspec.jobParameters += ' input%sFile=%s' % (itype, ','.join(job.inputdata.names))

        # Log files.
        lfspec = FileSpec()
        lfspec.lfn = '%s.job.log.tgz' % jspec.jobName
        lfspec.destinationDBlock = jspec.destinationDBlock
        lfspec.destinationSE  = jspec.destinationSE
        lfspec.dataset = jspec.destinationDBlock
        lfspec.type = 'log'
        jspec.addFile(lfspec)

        return jspec

from Ganga.GPIDev.Adapters.ApplicationRuntimeHandlers import allHandlers
allHandlers.add('ProdTrans', 'Panda', ProdTransPandaRTHandler)
