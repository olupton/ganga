###############################################################################
# Ganga Project. http://cern.ch/ganga
#
# $Id: AthenaPandaRTHandler.py,v 1.32 2009-05-29 13:27:14 dvanders Exp $
###############################################################################
# Athena LCG Runtime Handler
#
# ATLAS/ARDA

import os, sys, pwd, commands, re, shutil, urllib, time, string, exceptions

from Ganga.Core.exceptions import ApplicationConfigurationError
from Ganga.GPIDev.Base import GangaObject
from Ganga.GPIDev.Schema import *
from Ganga.GPIDev.Lib.File import *
from Ganga.GPIDev.Adapters.IRuntimeHandler import IRuntimeHandler

from GangaAtlas.Lib.ATLASDataset import DQ2Dataset, DQ2OutputDataset
from GangaPanda.Lib.Panda.Panda import runPandaBrokerage, uploadSources, getLibFileSpecFromLibDS
from Ganga.Core import BackendError

from GangaAtlas.Lib.ATLASDataset.DQ2Dataset import dq2outputdatasetname
from GangaAtlas.Lib.ATLASDataset.DQ2Dataset import dq2_set_dataset_lifetime
from GangaAtlas.Lib.Credentials.ProxyHelper import getNickname
from GangaAtlas.Lib.ATLASDataset.DQ2Dataset import dq2_lock, dq2

from Ganga.Utility.GridShell import getShell
from GangaPanda.Lib.Panda.Panda import setChirpVariables


def getDBDatasets(jobO,trf,dbrelease):
    from pandatools import Client

    # get DB datasets
    dbrFiles  = {}
    dbrDsList = []
    if trf or dbrelease != '':
        if trf:
            # parse jobO for TRF
            tmpItems = jobO.split()
        else:
            # mimic a trf parameter to reuse following algorithm
            tmpItems = ['%DB='+dbrelease]
        # look for DBRelease
        for tmpItem in tmpItems:
            match = re.search('%DB=([^:]+):(.+)$',tmpItem)
            if match:
                tmpDbrDS  = match.group(1)
                tmpDbrLFN = match.group(2)
                # get files in the dataset
                if not tmpDbrDS in dbrDsList:
                    logger.info("Querying files in dataset:%s" % tmpDbrDS)
                    try:
                        tmpList = Client.queryFilesInDataset(tmpDbrDS,False)
                    except:
                        raise ApplicationConfigurationError(None,"ERROR : error while looking up dataset %s. Perhaps this dataset does not exist?"%tmpDbrDS)
                    # append
                    for tmpLFN,tmpVal in tmpList.iteritems():
                        dbrFiles[tmpLFN] = tmpVal
                    dbrDsList.append(tmpDbrDS)
                # check
                if not dbrFiles.has_key(tmpDbrLFN):
                    raise ApplicationConfigurationError(None,"ERROR : %s is not in %s"%(tmpDbrLFN,tmpDbrDS))
    return dbrFiles,dbrDsList


class AthenaPandaRTHandler(IRuntimeHandler):
    '''Athena Panda Runtime Handler'''


    def master_prepare(self,app,appconfig):
        '''Prepare the master job'''

        from pandatools import Client
        from pandatools import AthenaUtils
        from taskbuffer.JobSpec import JobSpec
        from taskbuffer.FileSpec import FileSpec


        job = app._getParent()
        logger.debug('AthenaPandaRTHandler master_prepare called for %s', job.getFQID('.')) 

        if job.backend.bexec and job.backend.nobuild:
            raise ApplicationConfigurationError(None,"Contradicting options: job.backend.bexec and job.backend.nobuild are both enabled.")

        # Switch on compilation flag if bexec is set or libds is empty
        if job.backend.bexec != '' or not job.backend.nobuild:
            app.athena_compile = True
            for sj in job.subjobs:
                sj.application.athena_compile = True
            logger.info('"job.backend.nobuild=False" or "job.backend.bexec" is set - Panda build job is enabled.')

        if job.backend.nobuild:
            app.athena_compile = False
            for sj in job.subjobs:
                sj.application.athena_compile = False
            logger.info('"job.backend.nobuild=True" or "--nobuild" chosen - Panda build job is switched off.')

        # set chirp variables
        if configPanda['chirpconfig'] or configPanda['chirpserver']:
            setChirpVariables()
            logger.debug( configPanda['chirpserver'] )
            logger.debug( configPanda['chirpconfig'] )

        # validate application
        if not app.atlas_release:
            raise ApplicationConfigurationError(None,"application.atlas_release is not set. Did you run application.prepare()")
        self.dbrelease = app.atlas_dbrelease
        if self.dbrelease != '' and self.dbrelease.find(':') == -1:
            raise ApplicationConfigurationError(None,"ERROR : invalid argument for DB Release. Must be 'DatasetName:FileName'")
        self.runConfig = AthenaUtils.ConfigAttr(app.atlas_run_config)
        for k in self.runConfig.keys():
            self.runConfig[k]=AthenaUtils.ConfigAttr(self.runConfig[k])
        if not app.atlas_run_dir:
            raise ApplicationConfigurationError(None,"application.atlas_run_dir is not set. Did you run application.prepare()")
        self.rundirectory = app.atlas_run_dir
        self.cacheVer = ''
        if app.atlas_project and app.atlas_production:
            self.cacheVer = "-" + app.atlas_project + "_" + app.atlas_production
        if not app.atlas_exetype in ['ATHENA','PYARA','ARES','ROOT','EXE', 'TRF']:
            raise ApplicationConfigurationError(None,"Panda backend supports only application.atlas_exetype in ['ATHENA','PYARA','ARES','ROOT','EXE', 'TRF']")
        if app.atlas_exetype == 'ATHENA' and not app.user_area.name and not job.backend.libds:
            raise ApplicationConfigurationError(None,'app.user_area.name is null')

        # validate inputdata
        if job.inputdata:
            if job.inputdata._name == 'DQ2Dataset':
                self.inputdatatype='DQ2'
                logger.info('Input dataset(s) %s',job.inputdata.dataset)
            elif job.inputdata._name == 'AMIDataset':
                self.inputdatatype='DQ2'
                job.inputdata.dataset = job.inputdata.search()
                logger.info('Input dataset(s) %s',job.inputdata.dataset)
            elif job.inputdata._name == 'EventPicking':
                self.inputdatatype='DQ2'
                logger.info('Input dataset(s) %s',job.inputdata.dataset)
            elif job.inputdata._name == 'ATLASTier3Dataset':
                self.inputdatatype='Tier3'
                logger.info('Input dataset is a Tier3 PFN list')
            else: 
                raise ApplicationConfigurationError(None,'Panda backend supports only inputdata=DQ2Dataset()')

            #if not app.athena_compile and (len(job.inputdata.dataset) > 1 or any(ds.endswith("/") for ds in job.inputdata.dataset)):
            #    logger.warning("Since this job is submitted to multiple sites; the builjob has to be executed. Enabling athena_compile.")
            #    app.athena_compile = True
        else:
            logger.info('Submitting without an input dataset.')

        # handle different atlas_exetypes
        self.job_options = ''
        if app.atlas_exetype == 'TRF':
            self.job_options += ' '.join([os.path.basename(fopt.name) for fopt in app.option_file])

            if not job.outputdata.outputdata:
                raise ApplicationConfigurationError(None,"job.outputdata.outputdata is required for atlas_exetype in ['PYARA','ARES','TRF','ROOT','EXE' ] and Panda backend")
            #raise ApplicationConfigurationError(None,"Sorry TRF on Panda backend not yet supported")

        
        elif app.atlas_exetype == 'ATHENA':
            
            if len(app.atlas_environment) > 0 and app.atlas_environment[0].find('DBRELEASE_OVERRIDE')==-1:
                logger.warning("Passing of environment variables to Athena using Panda not supported. Ignoring atlas_environment setting.")
                
            if job.outputdata.outputdata:
                raise ApplicationConfigurationError(None,"job.outputdata.outputdata must be empty if atlas_exetype='ATHENA' and Panda backend is used (outputs are auto-detected)")
            if app.options:
                if app.options.startswith('-c'):
                    self.job_options += ' %s ' % app.options
                else:
                    self.job_options += ' -c %s ' % app.options

                logger.warning('The value of j.application.options has been prepended with " -c " ')
                logger.warning('Please make sure to use proper quotes for the values of j.application.options !')

            self.job_options += ' '.join([os.path.basename(fopt.name) for fopt in app.option_file])

            # check for TAG compression
            if 'subcoll.tar.gz' in app.append_to_user_area:
                self.job_options = ' uncompress.py ' + self.job_options
                
        elif app.atlas_exetype in ['PYARA','ARES','ROOT','EXE']:

            if not job.outputdata.outputdata:
                raise ApplicationConfigurationError(None,"job.outputdata.outputdata is required for atlas_exetype in ['PYARA','ARES','TRF','ROOT','EXE' ] and Panda backend")
            self.job_options += ' '.join([os.path.basename(fopt.name) for fopt in app.option_file])

            # sort out environment variables
            env_str = ""
            if len(app.atlas_environment) > 0:
                for env_var in app.atlas_environment:
                    env_str += "export %s ; " % env_var
            else: 
                env_str = ""

            # below fixes issue with runGen -- job_options are executed by os.system when dbrelease is used, and by the shell otherwise
            if app.atlas_dbrelease: 
                if job.backend.requirements.usecommainputtxt:
                    input_str = '/bin/echo %IN > input.txt; cat input.txt; '
                else:
                    input_str = '/bin/echo %IN | sed \'s/,/\\\\\\n/g\' > input.txt; cat input.txt; '
                if app.atlas_exetype == 'PYARA':
                    self.job_options = env_str + input_str + ' python ' + self.job_options
                elif app.atlas_exetype == 'ARES':
                    self.job_options = env_str + input_str + ' athena.py ' + self.job_options
                elif app.atlas_exetype == 'ROOT':
                    self.job_options = env_str + input_str + ' root -b -q ' + self.job_options
                elif app.atlas_exetype == 'EXE':
                    self.job_options = env_str + input_str + self.job_options
            else:
                if job.backend.requirements.usecommainputtxt:
                    input_str = '/bin/echo %IN > input.txt; cat input.txt; '
                else:
                    input_str = '/bin/echo %IN | sed \'s/,/\\\\n/g\' > input.txt; cat input.txt; '
                if app.atlas_exetype == 'PYARA':
                    self.job_options = env_str + input_str + ' python ' + self.job_options
                elif app.atlas_exetype == 'ARES':
                    self.job_options = env_str + input_str + ' athena.py ' + self.job_options
                elif app.atlas_exetype == 'ROOT':
                    self.job_options = env_str + input_str + ' root -b -q ' + self.job_options
                elif app.atlas_exetype == 'EXE':
                    self.job_options = env_str + input_str + self.job_options

            if app.options:
                self.job_options += ' %s ' % app.options

        if self.job_options == '':
            raise ApplicationConfigurationError(None,"No Job Options found!")
        logger.info('Running job options: %s'%self.job_options)

        # validate inputdata
        if job.inputdata:
            if job.inputdata._name == 'DQ2Dataset':
                self.inputdatatype='DQ2'
                logger.info('Input dataset(s) %s',job.inputdata.dataset)
            elif job.inputdata._name == 'AMIDataset':
                self.inputdatatype='DQ2'
                job.inputdata.dataset = job.inputdata.search()
                logger.info('Input dataset(s) %s',job.inputdata.dataset)
            elif job.inputdata._name == 'EventPicking':
                self.inputdatatype='DQ2'
                logger.info('Input dataset(s) %s',job.inputdata.dataset)
            elif job.inputdata._name == 'ATLASTier3Dataset':
                self.inputdatatype='Tier3'
                logger.info('Input dataset is a Tier3 PFN list')
            else: 
                raise ApplicationConfigurationError(None,'Panda backend supports only inputdata=DQ2Dataset()')
        else:
            self.inputdatatype='None'
            logger.info('Proceeding without an input dataset.')

        # run brokerage here if not splitting'
        if self.inputdatatype=='DQ2':
            if not job.splitter:
                runPandaBrokerage(job)
            elif job.splitter._name <> 'DQ2JobSplitter' and job.splitter._name <> 'AnaTaskSplitterJob':
                raise ApplicationConfigurationError(None,'Splitting with Panda+DQ2Dataset requires DQ2JobSplitter')
        elif self.inputdatatype=='Tier3':
            if job.splitter and job.splitter._name != 'ATLASTier3Splitter':
                raise ApplicationConfigurationError(None,'Splitting with Panda+ATLASTier3Dataset requires ATLASTier3Splitter')
            if job.backend.site == 'AUTO':
                raise ApplicationConfigurationError(None,'Panda+ATLASTier3Dataset requires a specified backend.site')
            job.backend.requirements.cloud = Client.PandaSites[job.backend.site]['cloud']
        elif self.inputdatatype == 'None':
            runPandaBrokerage(job)
            
        if len(job.subjobs) == 0 and job.backend.site == 'AUTO':
            raise ApplicationConfigurationError(None,'Error: backend.site=AUTO after brokerage. Report to DA Help Forum')
        
        # handle the output dataset
        if job.outputdata:
            if job.outputdata._name <> 'DQ2OutputDataset':
                raise ApplicationConfigurationError(None,'Panda backend supports only DQ2OutputDataset')
        else:
            logger.info('Adding missing DQ2OutputDataset')
            job.outputdata = DQ2OutputDataset()

        # get the list of sites that this jobset will run at
        sitesdict = {}
        bjsites = []
        if job.subjobs:
            for sj in job.subjobs:
                sitesdict[sj.backend.site] = 1
            bjsites = sitesdict.keys()
        else:
            bjsites = [job.backend.site]

        # validate the output dataset name (and make it a container)
        job.outputdata.datasetname,outlfn = dq2outputdatasetname(job.outputdata.datasetname, job.id, job.outputdata.isGroupDS, job.outputdata.groupname)
        if not job.outputdata.datasetname.endswith('/'):
            job.outputdata.datasetname+='/'

        # check if this container exists
        res = Client.getDatasets(job.outputdata.datasetname)
        if not job.outputdata.datasetname in res.keys():
            # create the container
            try:
                Client.createContainer(job.outputdata.datasetname,False)
            except exceptions.SystemExit:
                raise BackendError('Panda','Exception in Client.createContainer %s: %s %s'%(job.outputdata.datasetname,sys.exc_info()[0],sys.exc_info()[1]))
            logger.info('Created output container %s'%job.outputdata.datasetname)
        else:
            logger.warning('Adding datasets to already existing container %s' % job.outputdata.datasetname)
            
        # store the lib datasts
        self.libDatasets = {}
        self.libraries = {}
        self.indivOutDsList = []
        for site in bjsites:
            self.outDsLocation = Client.PandaSites[site]['ddm']

            tmpDSName = job.outputdata.datasetname[0:-1] + ".%s"%site

            try:
                Client.addDataset(tmpDSName,False,location=self.outDsLocation)
                logger.info('Output dataset %s registered at %s'%(tmpDSName,self.outDsLocation))
                dq2_set_dataset_lifetime(tmpDSName, self.outDsLocation)
                self.indivOutDsList.append(tmpDSName)
                # add the DS to the container
                Client.addDatasetsToContainer(job.outputdata.datasetname,[tmpDSName],False)
            except exceptions.SystemExit:
                raise BackendError('Panda','Exception in adding dataset %s: %s %s'%(tmpDSName,sys.exc_info()[0],sys.exc_info()[1]))

            # handle the libds
            if job.backend.libds:
                self.libDatasets[site] = job.backend.libds
                self.fileBO = getLibFileSpecFromLibDS(self.libDatasets[site])
                self.libraries[site] = self.fileBO.lfn
            else:
                self.libDatasets[site]= tmpDSName+'.lib'
                self.libraries[site] = '%s.tgz' % self.libDatasets[site]
                if not job.backend.nobuild:
                    try:
                        Client.addDataset(self.libDatasets[site],False,location=self.outDsLocation)
                        dq2_set_dataset_lifetime(self.libDatasets[site], self.outDsLocation)
                        logger.info('Lib dataset %s registered at %s'%(self.libDatasets[site],self.outDsLocation))
                    except exceptions.SystemExit:
                        raise BackendError('Panda','Exception in Client.addDataset %s: %s %s'%(self.libDatasets[site],sys.exc_info()[0],sys.exc_info()[1]))

        # add extOutFiles
        self.extOutFile = []
        for tmpName in job.outputdata.outputdata:
            if tmpName != '':
                self.extOutFile.append(tmpName)
        for tmpName in job.backend.extOutFile:
            if tmpName != '':
                self.extOutFile.append(tmpName)

        # validate dbrelease
        self.dbrFiles,self.dbrDsList = getDBDatasets(self.job_options,'',self.dbrelease)

        # Add inputsandbox to user_area
        if job.inputsandbox:
            logger.warning("Submitting Panda job with inputsandbox. This may slow the submission slightly.")
            inpw = job.getInputWorkspace()
            self.inputsandbox = inpw.getPath('sources.%s.tar' % commands.getoutput('uuidgen'))

            if app.user_area.name:
                rc, output = commands.getstatusoutput('cp %s %s.gz' % (app.user_area.name, self.inputsandbox))
                if rc:
                    logger.error('Copying user_area failed with status %d',rc)
                    logger.error(output)
                    raise ApplicationConfigurationError(None,'Packing inputsandbox failed.')
                rc, output = commands.getstatusoutput('gunzip %s.gz' % (self.inputsandbox))
                if rc:
                    logger.error('Unzipping user_area failed with status %d',rc)
                    logger.error(output)
                    raise ApplicationConfigurationError(None,'Packing inputsandbox failed.')

            for fname in [os.path.abspath(f.name) for f in job.inputsandbox]:
                fname.rstrip(os.sep)
                path = os.path.dirname(fname)
                fn = os.path.basename(fname)

                #app.atlas_run_dir
                # get Athena versions
                rc, out = AthenaUtils.getAthenaVer()
                # failed
                if not rc:
                    #raise ApplicationConfigurationError(None, 'CMT could not parse correct environment ! \n Did you start/setup ganga in the run/ or cmt/ subdirectory of your athena analysis package ?')
                    logger.warning("CMT could not parse correct environment for inputsandbox - will use the atlas_run_dir as default")
                    
                    # as we don't have to be in the run dir now, create a copy of the run_dir directory structure and use that
                    input_dir = os.path.dirname(self.inputsandbox)
                    run_path = "%s/sbx_tree/%s" % (input_dir, app.atlas_run_dir)
                    rc, output = commands.getstatusoutput("mkdir -p %s" % run_path)
                    if not rc:
                        # copy this sandbox file
                        rc, output = commands.getstatusoutput("cp %s %s" % (fname, run_path))
                        if not rc:
                            path = os.path.join(input_dir, 'sbx_tree')
                            fn = os.path.join(app.atlas_run_dir, fn)
                        else:
                            raise ApplicationConfigurationError(None, "Couldn't copy file %s to recreate run_dir for input sandbox" % fname)
                    else:
                        raise ApplicationConfigurationError(None, "Couldn't create directory structure to match run_dir %s for input sandbox" % run_path)

                else:
                    userarea = out['workArea']

                    # strip the path from the filename if present in the userarea
                    ua = os.path.abspath(userarea)
                    if ua in path:
                        fn = fname[len(ua)+1:]
                        path = ua

                rc, output = commands.getstatusoutput('tar -h -r -f %s -C %s %s' % (self.inputsandbox, path, fn))
                if rc:
                    logger.error('Packing inputsandbox failed with status %d',rc)
                    logger.error(output)
                    raise ApplicationConfigurationError(None,'Packing inputsandbox failed.')

            # remove sandbox tree if created
            if "sbx_tree" in os.listdir(os.path.dirname(self.inputsandbox)):                
                rc, output = commands.getstatusoutput("rm -r %s/sbx_tree" % os.path.dirname(self.inputsandbox))
                if rc:
                    raise ApplicationConfigurationError(None, "Couldn't remove directory structure used for input sandbox")
                
            rc, output = commands.getstatusoutput('gzip %s' % (self.inputsandbox))
            if rc:
                logger.error('Packing inputsandbox failed with status %d',rc)
                logger.error(output)
                raise ApplicationConfigurationError(None,'Packing inputsandbox failed.')
            self.inputsandbox += ".gz"
        else:
            self.inputsandbox = app.user_area.name

        # upload sources
        if self.inputsandbox and not job.backend.libds:
            uploadSources(os.path.dirname(self.inputsandbox),os.path.basename(self.inputsandbox))

        # create build job for each needed site
        if app.athena_compile:
            if not job.backend.libds:
                logger.info("Creating a build job for %s"%','.join(bjsites))
            bjspecs=[]
            for bjsite in bjsites:
                tmpLibDS = job.outputdata.datasetname+'.lib'
                jspec = JobSpec()
                jspec.jobDefinitionID   = job.id

                if job.backend.jobSpec.has_key('provenanceID'):
                    jspec.jobExecutionID =  job.backend.jobSpec['provenanceID']
                
                jspec.jobName           = commands.getoutput('uuidgen')
                jspec.AtlasRelease      = 'Atlas-%s' % app.atlas_release
                jspec.homepackage       = 'AnalysisTransforms'+self.cacheVer#+nightVer
                if job.backend.bexec != '':
                    jspec.transformation    = '%s/buildGen-00-00-01' % Client.baseURLSUB
                else:
                    jspec.transformation    = '%s/buildJob-00-00-03' % Client.baseURLSUB
                if Client.isDQ2free(bjsite):
                    jspec.destinationDBlock = '%s/%s' % (job.outputdata.datasetname,self.libDatasets[bjsite])
                    jspec.destinationSE     = 'local'
                else:
                    jspec.destinationDBlock = self.libDatasets[bjsite]
                    jspec.destinationSE     = bjsite
                jspec.prodSourceLabel   = configPanda['prodSourceLabelBuild']
                jspec.processingType    = configPanda['processingType']
                jspec.assignedPriority  = configPanda['assignedPriorityBuild']
                jspec.computingSite     = bjsite
                jspec.cloud             = Client.PandaSites[bjsite]['cloud']
                jspec.jobParameters     = '-o %s' % (self.libraries[bjsite])
                if self.inputsandbox:
                    jspec.jobParameters     += ' -i %s' % (os.path.basename(self.inputsandbox))
                matchURL = re.search('(http.*://[^/]+)/',Client.baseURLSSL)
                if matchURL:
                    jspec.jobParameters += ' --sourceURL %s' % matchURL.group(1)
                jspec.cmtConfig         = AthenaUtils.getCmtConfig(athenaVer=app.atlas_release)
                if job.backend.bexec != '':
                    jspec.jobParameters += ' --bexec "%s" ' % urllib.quote(job.backend.bexec)
                    jspec.jobParameters += ' -r %s ' % '.'

                fout = FileSpec()
                fout.lfn  = self.libraries[bjsite]
                fout.type = 'output'
                fout.dataset = self.libDatasets[bjsite]
                fout.destinationDBlock = self.libDatasets[bjsite]
                jspec.addFile(fout)

                flog = FileSpec()
                flog.lfn = '%s.log.tgz' % self.libDatasets[bjsite]
                flog.type = 'log'
                flog.dataset = self.libDatasets[bjsite]
                flog.destinationDBlock = self.libDatasets[bjsite]
                if configPanda['chirpconfig']:
                    flog.dispatchDBlockToken = configPanda['chirpconfig']
                jspec.addFile(flog)
                
                bjspecs.append(jspec)
        
            return bjspecs
        else:
            return []

    def prepare(self,app,appsubconfig,appmasterconfig,jobmasterconfig):
        '''prepare the subjob specific configuration'''

        from pandatools import Client
        from pandatools import AthenaUtils
        from taskbuffer.JobSpec import JobSpec
        from taskbuffer.FileSpec import FileSpec

 
        job = app._getParent()
        masterjob = job._getRoot()

        logger.debug('AthenaPandaRTHandler prepare called for %s', job.getFQID('.'))

#       in case of a simple job get the dataset content, otherwise subjobs are filled by the splitter
        if job.inputdata and self.inputdatatype=='DQ2' and not masterjob.subjobs:
            if not job.inputdata.names:
                for guid, lfn in job.inputdata.get_contents():
                    job.inputdata.guids.append(guid)
                    job.inputdata.names.append(lfn)

        job.backend.actualCE = job.backend.site
        job.backend.requirements.cloud = Client.PandaSites[job.backend.site]['cloud']

#       if no outputdata are given
        if not job.outputdata:
            job.outputdata = DQ2OutputDataset()
        job.outputdata.datasetname = masterjob.outputdata.datasetname[0:-1]+'.%s'%job.backend.site 

        if job.inputdata and self.inputdatatype=='DQ2':
            if len(job.inputdata.dataset) > 1:
                raise ApplicationConfigurationError(None,'Multiple input datasets per subjob not supported. Use a container dataset?')

        jspec = JobSpec()
        jspec.jobDefinitionID   = masterjob.id

        if job.backend.jobSpec.has_key('provenanceID'):
            jspec.jobExecutionID =  job.backend.jobSpec['provenanceID']
        
        jspec.jobName           = commands.getoutput('uuidgen')  
        jspec.AtlasRelease      = 'Atlas-%s' % app.atlas_release
        jspec.homepackage       = 'AnalysisTransforms'+self.cacheVer#+nightVer
        if app.atlas_exetype in ['PYARA','ARES','ROOT','EXE']:
            jspec.transformation    = '%s/runGen-00-00-02' % Client.baseURLSUB
        else:
            jspec.transformation    = '%s/runAthena-00-00-11' % Client.baseURLSUB
        if job.inputdata and self.inputdatatype=='DQ2' and (not job.inputdata.tag_info or app.atlas_exetype in ['PYARA','ARES','ROOT','EXE']):
            jspec.prodDBlock    = job.inputdata.dataset[0]
        else:
            jspec.prodDBlock    = 'NULL'
        jspec.destinationDBlock = job.outputdata.datasetname
        if Client.isDQ2free(job.backend.site):
            jspec.destinationSE = 'local'
        elif job.outputdata.location:# and (not job._getRoot().subjobs or job.id == 0):
#            logger.warning('User defined output locations not supported. Use DaTRI: https://twiki.cern.ch/twiki/bin/view/Atlas/DataTransferRequestInterface')
            jspec.destinationSE = job.outputdata.location
        else:
            jspec.destinationSE = job.backend.site
        jspec.prodSourceLabel   = configPanda['prodSourceLabelRun']
        jspec.processingType    = configPanda['processingType']
        jspec.assignedPriority  = configPanda['assignedPriorityRun']
        jspec.cloud             = job.backend.requirements.cloud
        jspec.computingSite     = job.backend.site
        if job.backend.requirements.memory != -1:
            jspec.minRamCount = job.backend.requirements.memory
        if job.backend.requirements.cputime != -1:
            jspec.maxCpuCount = job.backend.requirements.cputime
        jspec.cmtConfig         = AthenaUtils.getCmtConfig(athenaVer=app.atlas_release)

#       library (source files)
        if job.backend.libds:
            flib = FileSpec()
            flib.lfn            = self.fileBO.lfn
            flib.GUID           = self.fileBO.GUID
            flib.type           = 'input'
            flib.status         = self.fileBO.status
            flib.dataset        = self.fileBO.destinationDBlock
            flib.dispatchDBlock = self.fileBO.destinationDBlock
            jspec.addFile(flib)
        elif job.application.athena_compile:
            flib = FileSpec()
            flib.lfn            = self.libraries[job.backend.site]
            flib.type           = 'input'
            flib.dataset        = self.libDatasets[job.backend.site]
            flib.dispatchDBlock = self.libDatasets[job.backend.site]
            jspec.addFile(flib)

#       input files FIXME: many more input types
        if job.inputdata and self.inputdatatype=='DQ2':
            for guid, lfn in zip(job.inputdata.guids,job.inputdata.names): 
                finp = FileSpec()
                finp.lfn            = lfn
                finp.GUID           = guid
                #            finp.fsize =
                #            finp.md5sum =
                finp.dataset        = job.inputdata.dataset[0]
                finp.prodDBlock     = job.inputdata.dataset[0]
                finp.dispatchDBlock = job.inputdata.dataset[0]
                finp.type           = 'input'
                finp.status         = 'ready'
                jspec.addFile(finp)
                
            if len(job.inputdata.tagdataset) != 0:
                # add the TAG files
                tag_contents = job.inputdata.get_tag_contents(size=True)
                tag_files = map(lambda x: x[1][0],tag_contents)
                tag_guids = map(lambda x: x[0],tag_contents)

                for guid, lfn in zip(tag_guids,tag_files): 
                    finp = FileSpec()
                    finp.lfn            = lfn
                    finp.GUID           = guid
                    #            finp.fsize =
                    #            finp.md5sum =
                    finp.dataset        = job.inputdata.tagdataset[0]
                    finp.prodDBlock     = job.inputdata.tagdataset[0]
                    finp.dispatchDBlock = job.inputdata.tagdataset[0]
                    finp.type           = 'input'
                    finp.status         = 'ready'
                    jspec.addFile(finp)

            
            if job.inputdata.tag_info:
                # add the TAG files
                tag_files = job.inputdata.tag_info.keys()
                tag_guids = []
                for tf in job.inputdata.tag_info.keys():
                    tag_guids.append( job.inputdata.tag_info[tf]['guid'] )
                    
                for guid, lfn in zip(tag_guids,tag_files):
                    finp = FileSpec()
                    finp.lfn            = lfn
                    finp.GUID           = guid
                    #            finp.fsize =
                    #            finp.md5sum =
                    finp.dataset        = job.inputdata.tag_info[lfn]['dataset']  #job.inputdata.tagdataset[0]
                    finp.prodDBlock     = job.inputdata.tag_info[lfn]['dataset'] #job.inputdata.tagdataset[0]
                    finp.dispatchDBlock = job.inputdata.tag_info[lfn]['dataset'] #job.inputdata.tagdataset[0]
                    finp.type           = 'input'
                    finp.status         = 'ready'
                    jspec.addFile(finp)
                    
#       output files
        outMap = {}
        AthenaUtils.convertConfToOutput(self.runConfig,jspec,outMap,job.backend.individualOutDS,self.extOutFile,masterjob.outputdata.datasetname)
        for file in jspec.Files:
            if file.type in ['output', 'log'] and configPanda['chirpconfig']:
                file.dispatchDBlockToken = configPanda['chirpconfig']
                logger.debug('chirp file %s',file)
      
        subjobOutputLocation = Client.PandaSites[job.backend.site]['ddm']
        jspec.destinationDBlock = masterjob.outputdata.datasetname
 
        if job.backend.individualOutDS:
            for f in jspec.Files:
                if f.type in ['output','log']:
                    if not f.dataset in self.indivOutContList:
                        try:
                            logger.info('Creating output container %s'%f.dataset)
                            Client.createContainer(f.dataset,False)
                            self.indivOutContList.append(f.dataset)
                        except exceptions.SystemExit:
                            raise BackendError('Panda','Exception in Client.createContainer %s: %s %s'%(f.dataset,sys.exc_info()[0],sys.exc_info()[1]))
                    if not f.destinationDBlock in self.indivOutDsList:
                        try:
                            logger.info('Creating dataset %s and adding to %s'%(f.destinationDBlock,f.dataset))
                            Client.addDataset(f.destinationDBlock,False,location=subjobOutputLocation)
                            dq2_set_dataset_lifetime(f.destinationDBlock, subjobOutputLocation)
                            self.indivOutDsList.append(f.destinationDBlock)
                            Client.addDatasetsToContainer(f.dataset,[f.destinationDBlock],False)
                        except exceptions.SystemExit:
                            raise BackendError('Panda','Exception in Client.addDataset %s: %s %s'%(f.dataset,sys.exc_info()[0],sys.exc_info()[1]))

#       job parameters
        param = ''

        # FIXME if not options.nobuild:
        if app.athena_compile:
            param =  '-l %s ' % self.libraries[job.backend.site]
        else:
            param += '-a %s ' % os.path.basename(self.inputsandbox)

        param += '-r %s ' % self.rundirectory
        # set jobO parameter
        if app.atlas_exetype in ['PYARA','ARES','ROOT','EXE']:
            param += '-j "" -p "%s" ' % self.job_options
        elif app.atlas_exetype in ['TRF']:
            #param += '-j "%s" ' % urllib.quote(app.options)
            pass
        else:
            param += '-j "%s" ' % urllib.quote(self.job_options)
        if app.atlas_exetype == 'ARES' or (app.atlas_exetype in ['PYARA','ROOT','EXE'] and app.useAthenaPackages):
            param += '--useAthenaPackages '
        # DBRelease
        if self.dbrelease != '' and not app.atlas_exetype in [ 'TRF' ]:
            tmpItems = self.dbrelease.split(':')
            tmpDbrDS  = tmpItems[0]
            tmpDbrLFN = tmpItems[1]
            # instantiate  FileSpec
            fileName = tmpDbrLFN
            vals     = self.dbrFiles[tmpDbrLFN]
            file = FileSpec()
            file.lfn            = fileName
            file.GUID           = vals['guid']
            file.fsize          = vals['fsize']
            file.md5sum         = vals['md5sum']
            file.dataset        = tmpDbrDS
            file.prodDBlock     = tmpDbrDS
            file.dispatchDBlock = tmpDbrDS
            file.type       = 'input'
            file.status     = 'ready'
            jspec.addFile(file)
            # set DBRelease parameter
            param += '--dbrFile %s ' % file.lfn

        if job.inputdata:
            # check for ELSSI files
            input_files = job.inputdata.names
            if self.inputdatatype == 'DQ2' and job.inputdata.tag_info:
                
                # tell Panda what files are TAG and what aren't                
                input_files += job.inputdata.tag_info.keys()
                param += '-i "%s" ' % input_files
                param += '--tagFileList %s ' % ','.join(job.inputdata.tag_info.keys())
                param += '--guidBoundary "%s" ' % job.inputdata.guids
                
                # set the coll name
                if self.runConfig.input.collRefName:
                    param += '--collRefName %s ' % self.runConfig.input.collRefName
                else:
                    # get coll ref from input data
                    if input_files[0].find("AOD") != -1:
                        param += '--collRefName StreamAOD_ref'
                    elif input_files[0].find("ESD") != -1:
                        param += '--collRefName StreamESD_ref'
                    elif input_files[0].find("RAW") != -1:
                        param += '--collRefName StreamRAW_ref'

                # sort out TAG use for exe types other than just athena - TRF dealt with below
                if app.atlas_exetype in ['PYARA','ARES','ROOT','EXE']:
                    self.job_options.replace("%IN", "$MY_INPUT_FILES")
                    self.job_options = "echo -e \"from commands import getstatusoutput\\nrc,o=getstatusoutput('ls pre_*-????-*-*-*.py')\\n__import__(o.split()[0][:-3])\\nfrom AthenaCommon.AthenaCommonFlags import athenaCommonFlags\\nopen('__input_files.txt', 'w').write(','.join(athenaCommonFlags.FilesInput() ))\" > __my_conv.py ; python __my_conv.py ; export MY_INPUT_FILES=`cat __input_files.txt` ; " + self.job_options
                
            elif self.inputdatatype == 'DQ2' and len(job.inputdata.tagdataset) != 0:
                # tell Panda what files are TAG and what aren't
                tag_contents = job.inputdata.get_tag_contents(size=True)
                tag_files = map(lambda x: x[1][0],tag_contents)
                input_files += tag_files
                param += '-i "%s" ' % input_files
                param += '--tagFileList %s ' % ','.join(tag_files)
                param += '--guidBoundary "%s" ' % job.inputdata.guids
                
                # set the coll name
                if self.runConfig.input.collRefName:
                    param += '--collRefName %s ' % self.runConfig.input.collRefName
                else:
                    # get coll ref from input data
                    if input_files[0].find("AOD") != -1:
                        param += '--collRefName StreamAOD_ref'
                    elif input_files[0].find("ESD") != -1:
                        param += '--collRefName StreamESD_ref'
                    elif input_files[0].find("RAW") != -1:
                        param += '--collRefName StreamRAW_ref'

                # sort out TAG use for exe types other than just athena - TRF dealt with below
                if app.atlas_exetype in ['PYARA','ARES','ROOT','EXE']:
                    self.job_options.replace("%IN", "$MY_INPUT_FILES")
                    self.job_options = "echo -e \"from commands import getstatusoutput\\nrc,o=getstatusoutput('ls pre_*-????-*-*-*.py')\\n__import__(o.split()[0][:-3])\\nfrom AthenaCommon.AthenaCommonFlags import athenaCommonFlags\\nopen('__input_files.txt', 'w').write(','.join(athenaCommonFlags.FilesInput() ))\" > __my_conv.py ; python __my_conv.py ; export MY_INPUT_FILES=`cat __input_files.txt` ; " + self.job_options
                    
            if not app.atlas_exetype in ['TRF']:
                param += '-i "%s" ' % input_files
        else:
            param += '-i "[]" '

        # TRFs
        if app.atlas_exetype in ['TRF']:
            tmpJobO = app.options

            # sort out TAG use for exe types other than just athena
            if self.inputdatatype == 'DQ2' and (len(job.inputdata.tagdataset) != 0 or job.inputdata.tag_info):
                tmpJobO = tmpJobO.replace("%IN", "$MY_INPUT_FILES")
                tmpJobO = "echo -e \"from commands import getstatusoutput\\nrc,o=getstatusoutput('ls pre_*-????-*-*-*.py')\\n__import__(o.split()[0][:-3])\\nfrom AthenaCommon.AthenaCommonFlags import athenaCommonFlags\\nopen('__input_files.txt', 'w').write(','.join(athenaCommonFlags.FilesInput() ))\" > __my_conv.py ; python __my_conv.py ; export MY_INPUT_FILES=`cat __input_files.txt` ; " + tmpJobO
                
            # output
            tmpOutMap = []
            for tmpName,tmpLFN in outMap['IROOT']:
                tmpJobO = tmpJobO.replace('%OUT.' + tmpName,tmpName)
                # set correct name in outMap
                tmpOutMap.append((tmpName,tmpLFN))
            outMap['IROOT'] = tmpOutMap 
            # input 
            minList = []
            cavList = []
            bhaloList = []
            bgasList = []
            inPattList = [('%IN', input_files ),('%MININ',minList),('%CAVIN',cavList),('%BHIN',bhaloList),('%BGIN',bgasList)]    
            for tmpPatt,tmpInList in inPattList:
                if tmpJobO.find(tmpPatt) != -1 and len(tmpInList) > 0:
                    tmpJobO = AthenaUtils.replaceParam(tmpPatt,tmpInList,tmpJobO)
 
           # DBRelease
            tmpItems = tmpJobO.split()
            if self.dbrelease != '':
                # mimic a trf parameter to reuse following algorithm
                tmpItems += ['%DB='+self.dbrelease]
            for tmpItem in tmpItems:
                match = re.search('%DB=([^:]+):(.+)$',tmpItem)
                if match:
                    tmpDbrDS  = match.group(1)
                    tmpDbrLFN = match.group(2)
                    # skip if it is already extracted
                    if tmpDbrLFN in input_files:
                        continue
                    # instantiate  FileSpec
                    fileName = tmpDbrLFN
                    vals     = self.dbrFiles[tmpDbrLFN]
                    file = FileSpec()
                    file.lfn            = fileName
                    file.GUID           = vals['guid']
                    file.fsize          = vals['fsize']
                    file.md5sum         = vals['md5sum']
                    file.dataset        = tmpDbrDS
                    file.prodDBlock     = tmpDbrDS
                    file.dispatchDBlock = tmpDbrDS
                    file.type       = 'input'
                    file.status     = 'ready'
                    jspec.addFile(file)
                    input_files.append(fileName)
                    # replace parameters
                    tmpJobO = tmpJobO.replace(match.group(0),tmpDbrLFN)

            param += ' -j "%s" ' % urllib.quote(tmpJobO) 

            param += ' -i "%s" ' % input_files

            param += ' -m "[]" ' #%minList FIXME
            param += ' -n "[]" ' #%cavList FIXME
            param += ' --trf ' 

            # direct access site ?
            from pandatools import PsubUtils
            inTRF = True
            inARA = False
            inBS = False
            if self.runConfig.input and self.runConfig.input.inBS:
                inBS = True
            isDirectAccess = PsubUtils.isDirectAccess(job.backend.site, inBS, inTRF, inARA)

            #if not isDirectAccess:
            if not isDirectAccess and (( self.inputdatatype != 'DQ2' ) or (len(job.inputdata.tagdataset) == 0 and not job.inputdata.tag_info)):
                param += ' --useLocalIO '

        #param += '-m "[]" ' #%minList FIXME
        #param += '-n "[]" ' #%cavList FIXME
        #FIXME
        #if bhaloList != []:
        #    param += '--beamHalo "%s" ' % bhaloList
        #if bgasList != []:
        #    param += '--beamGas "%s" ' % bgasList
        if app.atlas_exetype in ['PYARA','ARES','ROOT','EXE']:
            outMapNew={}
            for x in outMap.values():
                outMapNew.update(dict(x))
            param += '-o "%s" ' % outMapNew
        else:
            param += '-o "%s" ' % outMap

        if (self.runConfig.input and self.runConfig.input.inColl) or param.find('--collRefName') != -1: 
            param += '-c '
        if self.runConfig.input and self.runConfig.input.inBS: 
            param += '-b '
        if self.runConfig.input and self.runConfig.input.backNavi: 
            param += '-e '
        #if self.config['shipinput']: 
        #    param += '--shipInput '
        #FIXME options.rndmStream
        nEventsToSkip = app.skip_events
        if app.atlas_exetype == 'ATHENA' and app.max_events > 0:
            param += '-f "theApp.EvtMax=%d;EventSelector.SkipEvents=%s" ' % (app.max_events,nEventsToSkip)

        #event picking 
        if len(app.run_event) >= 1:
            param += '--eventPickTxt=%s ' % app.run_event_file.split('/')[-1]

        # addPoolFC
        #if self.config['addPoolFC'] != "":
        #    param += '--addPoolFC %s ' % self.config['addPoolFC']
        # use corruption checker
        if job.backend.requirements.corCheck:
            param += '--corCheck '
        # disable to skip missing files
        if job.backend.requirements.notSkipMissing:
            param += '--notSkipMissing '
        # given PFN 
        #if self.config['pfnList'] != '':
        #    param += '--givenPFN '
        # create symlink for MC data
        #if self.config['mcData'] != '':
        #    param += '--mcData %s ' % self.config['mcData']
        # source URL
        matchURL = re.search("(http.*://[^/]+)/",Client.baseURLSSL)
        if matchURL != None:
            param += " --sourceURL %s " % matchURL.group(1)
        # use ARA 
#        if app.atlas_exetype in ['PYARA','ARES','ROOT']:
#            param += '--trf '
#            param += '--ara '
        if job.backend.accessmode == 'FILE_STAGER':
            param += '--accessmode=filestager '
        elif job.backend.accessmode == 'DIRECT':
            param += '--accessmode=direct '
        elif job.backend.accessmode == 'COPY2SCRATCH':
            param += '--accessmode=copy '
        if self.inputdatatype == 'Tier3' and not app.atlas_exetype in ['PYARA','ARES','ROOT','EXE']:
            param += '--givenPFN '
 
        jspec.jobParameters = param

        if app.atlas_exetype in ['TRF']:
            jspec.metadata = '--trf "%s" ' %( app.options)
        
        return jspec

from Ganga.GPIDev.Credentials import GridProxy
gridProxy = GridProxy()

from Ganga.GPIDev.Adapters.ApplicationRuntimeHandlers import allHandlers

for app in ['Athena', 'ExecutableDQ2', 'RootDQ2']:
    allHandlers.add(app,'Panda',AthenaPandaRTHandler)

from Ganga.Utility.Config import getConfig, ConfigError
configDQ2 = getConfig('DQ2')
configPanda = getConfig('Panda')

from Ganga.Utility.logging import getLogger
logger = getLogger()
