# Copyright (c) 2012 - 2014 EMC Corporation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os.path

from oslo.config import cfg
import six

from cinder import exception
from cinder.i18n import _, _LE, _LI, _LW
from cinder.openstack.common import log as logging
from cinder.volume.drivers.emc import emc_vmax_fast
from cinder.volume.drivers.emc import emc_vmax_https
from cinder.volume.drivers.emc import emc_vmax_masking
from cinder.volume.drivers.emc import emc_vmax_provision
from cinder.volume.drivers.emc import emc_vmax_provision_v3
from cinder.volume.drivers.emc import emc_vmax_utils


LOG = logging.getLogger(__name__)

CONF = cfg.CONF

try:
    import inspect

    import pywbem
    argspec = inspect.getargspec(pywbem.WBEMConnection.__init__)
    if any("ca_certs" in s for s in argspec.args):
        updatedPywbem = True
    else:
        updatedPywbem = False
    pywbemAvailable = True
except ImportError:
    pywbemAvailable = False

CINDER_EMC_CONFIG_FILE = '/etc/cinder/cinder_emc_config.xml'
CINDER_EMC_CONFIG_FILE_PREFIX = '/etc/cinder/cinder_emc_config_'
CINDER_EMC_CONFIG_FILE_POSTFIX = '.xml'
EMC_ROOT = 'root/emc'
POOL = 'storagetype:pool'
ARRAY = 'storagetype:array'
FASTPOLICY = 'storagetype:fastpolicy'
BACKENDNAME = 'volume_backend_name'
COMPOSITETYPE = 'storagetype:compositetype'
STRIPECOUNT = 'storagetype:stripecount'
MEMBERCOUNT = 'storagetype:membercount'
STRIPED = 'striped'
CONCATENATED = 'concatenated'
# V3
SLO = 'storagetype:slo'
WORKLOAD = 'storagetype:workload'
ISV3 = 'isV3'

emc_opts = [
    cfg.StrOpt('cinder_emc_config_file',
               default=CINDER_EMC_CONFIG_FILE,
               help='use this file for cinder emc plugin '
                    'config data'), ]

CONF.register_opts(emc_opts)


class EMCVMAXCommon(object):
    """Common class for SMI-S based EMC volume drivers.

    This common class is for EMC volume drivers based on SMI-S.
    It supports VNX and VMAX arrays.

    """

    stats = {'driver_version': '1.0',
             'free_capacity_gb': 0,
             'reserved_percentage': 0,
             'storage_protocol': None,
             'total_capacity_gb': 0,
             'vendor_name': 'EMC',
             'volume_backend_name': None}

    def __init__(self, prtcl, configuration=None):

        if not pywbemAvailable:
            LOG.info(_LI(
                'Module PyWBEM not installed.  '
                'Install PyWBEM using the python-pywbem package.'))

        self.protocol = prtcl
        self.configuration = configuration
        self.configuration.append_config_values(emc_opts)
        self.conn = None
        self.url = None
        self.user = None
        self.passwd = None
        self.masking = emc_vmax_masking.EMCVMAXMasking(prtcl)
        self.utils = emc_vmax_utils.EMCVMAXUtils(prtcl)
        self.fast = emc_vmax_fast.EMCVMAXFast(prtcl)
        self.provision = emc_vmax_provision.EMCVMAXProvision(prtcl)
        self.provisionv3 = emc_vmax_provision_v3.EMCVMAXProvisionV3(prtcl)

    def create_volume(self, volume):
        """Creates a EMC(VMAX) volume from a pre-existing storage pool.

        For a concatenated compositeType:
        If the volume size is over 240GB then a composite is created
        EMCNumberOfMembers > 1, otherwise it defaults to a non composite

        For a striped compositeType:
        The user must supply an extra spec to determine how many metas
        will make up the striped volume.If the meta size is greater than
        240GB an error is returned to the user. Otherwise the
        EMCNumberOfMembers is what the user specifies.

        :param volume: volume Object
        :returns: volumeInstance, the volume instance
        :raises: VolumeBackendAPIException
        """
        volumeSize = int(self.utils.convert_gb_to_bits(volume['size']))
        volumeName = volume['id']
        extraSpecs = self._initial_setup(volume)
        self.conn = self._get_ecom_connection()

        if extraSpecs[ISV3]:
            rc, volumeDict, storageSystemName = (
                self._create_v3_volume(volume, extraSpecs,
                                       volumeName, volumeSize))
        else:
            rc, volumeDict, storageSystemName = (
                self._create_composite_volume(volume, extraSpecs,
                                              volumeName, volumeSize))

        # if volume is created as part of a consistency group
        if 'consistencygroup_id' in volume and volume['consistencygroup_id']:
            cgName = self.utils.truncate_string(
                volume['consistencygroup_id'], 8)
            volumeInstance = self.utils.find_volume_instance(
                self.conn, volumeDict, volumeName)
            replicationService = (
                self.utils.find_replication_service(self.conn,
                                                    storageSystemName))
            cgInstanceName = (
                self._find_consistency_group(replicationService, cgName))
            self.provision.add_volume_to_cg(self.conn,
                                            replicationService,
                                            cgInstanceName,
                                            volumeInstance.path,
                                            cgName,
                                            volumeName)

        LOG.info(_LI("Leaving create_volume: %(volumeName)s  "
                     "Return code: %(rc)lu "
                     "volume dict: %(name)s"),
                 {'volumeName': volumeName,
                  'rc': rc,
                  'name': volumeDict})

        return volumeDict

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot.

        For VMAX, replace snapshot with clone.

        :param volume - volume Object
        :param snapshot - snapshot object
        :returns: cloneVolumeDict - the cloned volume dictionary
        """
        LOG.debug("Entering create_volume_from_snapshot")
        self._initial_setup(volume)
        self.conn = self._get_ecom_connection()
        snapshotInstance = self._find_lun(snapshot)
        storageSystem = snapshotInstance['SystemName']

        syncName = self.utils.find_sync_sv_by_target(
            self.conn, storageSystem, snapshotInstance, True)
        if syncName is not None:
            repservice = self.utils.find_replication_service(self.conn,
                                                             storageSystem)
            if repservice is None:
                exception_message = (_("Cannot find Replication Service to "
                                       "create volume for snapshot %s.")
                                     % snapshotInstance)
                raise exception.VolumeBackendAPIException(
                    data=exception_message)

            self.provision.delete_clone_relationship(
                self.conn, repservice, syncName)

        return self._create_cloned_volume(volume, snapshot)

    def create_cloned_volume(self, cloneVolume, sourceVolume):
        """Creates a clone of the specified volume.

        :param CloneVolume - clone volume Object
        :param sourceVolume - volume object
        :returns: cloneVolumeDict - the cloned volume dictionary
        """
        return self._create_cloned_volume(cloneVolume, sourceVolume)

    def delete_volume(self, volume):
        """Deletes a EMC(VMAX) volume

        :param volume: volume Object
        """
        LOG.info(_LI("Deleting Volume: %(volume)s"),
                 {'volume': volume['name']})

        rc, volumeName = self._delete_volume(volume)
        LOG.info(_LI("Leaving delete_volume: %(volumename)s  Return code: "
                     "%(rc)lu"),
                 {'volumename': volumeName,
                  'rc': rc})

    def create_snapshot(self, snapshot, volume):
        """Creates a snapshot.

        For VMAX, replace snapshot with clone

        :param snapshot: snapshot object
        :param volume: volume Object to create snapshot from
        :returns: cloneVolumeDict,the cloned volume dictionary
        """
        return self._create_cloned_volume(snapshot, volume, True)

    def delete_snapshot(self, snapshot, volume):
        """Deletes a snapshot.

        :param snapshot: snapshot object
        :param volume: volume Object to create snapshot from
        """
        LOG.info(_LI("Delete Snapshot: %(snapshotName)s "),
                 {'snapshotName': snapshot['name']})
        self._delete_snapshot(snapshot)

    def _remove_members(self, controllerConfigService,
                        volumeInstance, extraSpecs, connector):
        """This method unmaps a volume from a host.

        Removes volume from the Device Masking Group that belongs to
        a Masking View.
        Check if fast policy is in the extra specs, if it isn't we do
        not need to do any thing for FAST
        Assume that isTieringPolicySupported is False unless the FAST
        policy is in the extra specs and tiering is enabled on the array

        :param controllerConfigService: instance name of
                                  ControllerConfigurationService
        :param volume: volume Object
        :param extraSpecs: the volume extra specs
        :param connector: the connector object
        """
        volumeName = volumeInstance['ElementName']
        LOG.debug("Detaching volume %s", volumeName)
        try:
            fastPolicyName = extraSpecs[FASTPOLICY]
        except KeyError:
            fastPolicyName = None
        isV3 = extraSpecs[ISV3]
        return self.masking.remove_and_reset_members(
            self.conn, controllerConfigService, volumeInstance,
            fastPolicyName, volumeName, isV3, connector)

    def _unmap_lun(self, volume, connector):
        """Unmaps a volume from the host.

        :param volume: the volume Object
        :param connector: the connector Object
        :raises: VolumeBackendAPIException
        """
        extraSpecs = self._initial_setup(volume)
        volumename = volume['name']
        LOG.info(_LI("Unmap volume: %(volume)s"),
                 {'volume': volumename})

        device_info = self.find_device_number(volume, connector)
        device_number = device_info['hostlunid']
        if device_number is None:
            LOG.info(_LI("Volume %s is not mapped. No volume to unmap."),
                     (volumename))
            return

        vol_instance = self._find_lun(volume)
        storage_system = vol_instance['SystemName']

        configservice = self.utils.find_controller_configuration_service(
            self.conn, storage_system)
        if configservice is None:
            exception_message = (_("Cannot find Controller Configuration "
                                   "Service for storage system "
                                   "%(storage_system)s")
                                 % {'storage_system': storage_system})
            raise exception.VolumeBackendAPIException(data=exception_message)

        self._remove_members(configservice, vol_instance,
                             extraSpecs, connector)

    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns device and connection info.

        The volume may be already mapped, if this is so the deviceInfo tuple
        is returned.  If the volume is not already mapped then we need to
        gather information to either 1. Create an new masking view or 2.Add
        the volume to to an existing storage group within an already existing
        maskingview.

        The naming convention is the following:
        initiatorGroupName = OS-<shortHostName>-<shortProtocol>-IG
                             e.g OS-myShortHost-I-IG
        storageGroupName = OS-<shortHostName>-<poolName>-<shortProtocol>-SG
                           e.g OS-myShortHost-SATA_BRONZ1-I-SG
        portGroupName = OS-<target>-PG  The portGroupName will come from
                        the EMC configuration xml file.
                        These are precreated. If the portGroup does not exist
                        then a error will be returned to the user
        maskingView  = OS-<shortHostName>-<poolName>-<shortProtocol>-MV
                       e.g OS-myShortHost-SATA_BRONZ1-I-MV

        :param volume: volume Object
        :param connector: the connector Object
        :returns: deviceInfoDict, device information tuple
        :raises: VolumeBackendAPIException
        """
        extraSpecs = self._initial_setup(volume)

        volumeName = volume['name']
        LOG.info(_LI("Initialize connection: %(volume)s"),
                 {'volume': volumeName})
        self.conn = self._get_ecom_connection()
        deviceInfoDict = self._wrap_find_device_number(volume, connector)

        if ('hostlunid' in deviceInfoDict and
                deviceInfoDict['hostlunid'] is not None):
            isSameHost = self._is_same_host(connector, deviceInfoDict)
            if isSameHost:
                # Device is already mapped to same host so we will leave
                # the state as is.

                deviceNumber = deviceInfoDict['hostlunid']
                LOG.info(_LI("Volume %(volume)s is already mapped. "
                             "The device number is  %(deviceNumber)s. "),
                         {'volume': volumeName,
                          'deviceNumber': deviceNumber})
            else:
                deviceInfoDict = self._attach_volume(
                    volume, connector, extraSpecs, True)
        else:
            deviceInfoDict = self._attach_volume(volume, connector, extraSpecs)

        return deviceInfoDict

    def _attach_volume(
            self, volume, connector, extraSpecs, isLiveMigration=None):
        """Attach a volume to a host.

        If live migration is being undertaken then the volume
        remains attached to the source host.

        :params volume: the volume object
        :params connector: the connector object
        :params extraSpecs: the volume extra specs
        :param isLiveMigration: boolean, can be None
        :returns: deviceInfoDict
        """
        volumeName = volume['name']
        maskingViewDict = self._populate_masking_dict(
            volume, connector, extraSpecs)
        if isLiveMigration:
            maskingViewDict['isLiveMigration'] = True
        else:
            maskingViewDict['isLiveMigration'] = False

        rollbackDict = self.masking.get_or_create_masking_view_and_map_lun(
            self.conn, maskingViewDict)

        # Find host lun id again after the volume is exported to the host.
        deviceInfoDict = self.find_device_number(volume, connector)
        if 'hostlunid' not in deviceInfoDict:
            # Did not successfully attach to host,
            # so a rollback for FAST is required.
            LOG.error(_LE("Error Attaching volume %(vol)s. "),
                      {'vol': volumeName})
            if ((rollbackDict['fastPolicyName'] is not None) or
                    (rollbackDict['isV3'] is not None)):
                (self.masking
                    ._check_if_rollback_action_for_masking_required(
                        self.conn, rollbackDict))
            exception_message = ("Error Attaching volume %(vol)s."
                                 % {'vol': volumeName})
            raise exception.VolumeBackendAPIException(
                data=exception_message)

        return deviceInfoDict

    def _is_same_host(self, connector, deviceInfoDict):
        """Check if the host is the same.

        Check if the host to attach to is the same host
        that is already attached. This is necessary for
        live migration.

        :params connector: the connector object
        :params deviceInfoDict: the device information
        :returns: boolean True/False
        """
        if 'host' in connector:
            currentHost = connector['host']
            if ('maskingview' in deviceInfoDict and
                    deviceInfoDict['maskingview'] is not None):
                if currentHost in deviceInfoDict['maskingview']:
                    return True
        return False

    def _wrap_find_device_number(self, volume, connector):
        """Aid for unit testing

        :params volume: the volume Object
        :params connector: the connector Object
        :returns: deviceInfoDict
        """
        return self.find_device_number(volume, connector)

    def terminate_connection(self, volume, connector):
        """Disallow connection from connector.

        :params volume: the volume Object
        :params connectorL the connector Object
        """
        self._initial_setup(volume)

        volumename = volume['name']
        LOG.info(_LI("Terminate connection: %(volume)s"),
                 {'volume': volumename})

        self.conn = self._get_ecom_connection()
        self._unmap_lun(volume, connector)

    def extend_volume(self, volume, newSize):
        """Extends an existing volume.

        Prequisites:
        1. The volume must be composite e.g StorageVolume.EMCIsComposite=True
        2. The volume can only be concatenated
           e.g StorageExtent.IsConcatenated=True

        :params volume: the volume Object
        :params newSize: the new size to increase the volume to
        :raises: VolumeBackendAPIException
        """
        originalVolumeSize = volume['size']
        volumeName = volume['name']
        self._initial_setup(volume)
        self.conn = self._get_ecom_connection()
        volumeInstance = self._find_lun(volume)
        if volumeInstance is None:
            exceptionMessage = (_("Cannot find Volume: %(volumename)s. "
                                  "Extend operation.  Exiting....")
                                % {'volumename': volumeName})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        if int(originalVolumeSize) > int(newSize):
            exceptionMessage = (_(
                "Your original size: %(originalVolumeSize)s GB is greater "
                "than: %(newSize)s GB. Only Extend is supported. Exiting...")
                % {'originalVolumeSize': originalVolumeSize,
                   'newSize': newSize})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        additionalVolumeSize = six.text_type(
            int(newSize) - int(originalVolumeSize))
        additionalVolumeSize = self.utils.convert_gb_to_bits(
            additionalVolumeSize)

        # This is V2
        rc, modifiedVolumeDict = self._extend_composite_volume(
            volumeInstance, volumeName, newSize, additionalVolumeSize)

        # Check the occupied space of the new extended volume.
        extendedVolumeInstance = self.utils.find_volume_instance(
            self.conn, modifiedVolumeDict, volumeName)
        extendedVolumeSize = self.utils.get_volume_size(
            self.conn, extendedVolumeInstance)
        LOG.debug(
            "The actual volume size of the extended volume: %(volumeName)s "
            "is %(volumeSize)s",
            {'volumeName': volumeName,
             'volumeSize': extendedVolumeSize})

        # If the requested size and the actual size don't
        # tally throw an exception
        newSizeBits = self.utils.convert_gb_to_bits(newSize)
        diffVolumeSize = self.utils.compare_size(
            newSizeBits, extendedVolumeSize)
        if diffVolumeSize != 0:
            exceptionMessage = (_(
                "The requested size : %(requestedSize)s is not the same as "
                "resulting size: %(resultSize)s")
                % {'requestedSize': newSizeBits,
                   'resultSize': extendedVolumeSize})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        LOG.debug(
            "Leaving extend_volume: %(volumeName)s  "
            "Return code: %(rc)lu "
            "volume dict: %(name)s",
            {'volumeName': volumeName,
             'rc': rc,
             'name': modifiedVolumeDict})

        return modifiedVolumeDict

    def update_volume_stats(self):
        """Retrieve stats info.
        """
        if hasattr(self.configuration, 'cinder_emc_config_file'):
            emcConfigFileName = self.configuration.cinder_emc_config_file
        else:
            emcConfigFileName = self.configuration.safe_get(
                'cinder_emc_config_file')

        backendName = self.configuration.safe_get('volume_backend_name')
        LOG.debug(
            "Updating volume stats on file %(emcConfigFileName)s on "
            "backend %(backendName)s ",
            {'emcConfigFileName': emcConfigFileName,
             'backendName': backendName})

        if self.conn is None:
            self._set_ecom_credentials(emcConfigFileName)

        arrayName = self.utils.parse_array_name_from_file(emcConfigFileName)
        if arrayName is None:
            LOG.error(_LE(
                "Array Serial Number %(arrayName)s must be in the file "
                "%(emcConfigFileName)s "),
                {'arrayName': arrayName,
                 'emcConfigFileName': emcConfigFileName})

        poolName = self.utils.parse_pool_name_from_file(emcConfigFileName)
        if poolName is None:
            LOG.error(_LE(
                "PoolName %(poolName)s must be in the file "
                "%(emcConfigFileName)s "),
                {'poolName': poolName,
                 'emcConfigFileName': emcConfigFileName})

        isV3 = self.utils.isArrayV3(self.conn, arrayName)

        if isV3:
            location_info, total_capacity_gb, free_capacity_gb = (
                self._update_srp_stats(emcConfigFileName, arrayName, poolName))
        else:
            # This is V2
            location_info, total_capacity_gb, free_capacity_gb = (
                self._update_pool_stats(
                    emcConfigFileName, backendName, arrayName, poolName))

        data = {'total_capacity_gb': total_capacity_gb,
                'free_capacity_gb': free_capacity_gb,
                'reserved_percentage': 0,
                'QoS_support': False,
                'volume_backend_name': backendName or self.__class__.__name__,
                'vendor_name': "EMC",
                'driver_version': '2.1',
                'storage_protocol': 'unknown',
                'location_info': location_info,
                'consistencygroup_support': True}

        self.stats = data

        return self.stats

    def _update_srp_stats(self, emcConfigFileName, arrayName, poolName):
        """Update SRP stats

        :param emcConfigFileName: the EMC configuration file
        :param arrayName: the array
        :param poolName: the pool
        :returns: location_info, total_capacity_gb, free_capacity_gb
        """

        totalManagedSpaceGbs, remainingManagedSpaceGbs = (
            self.utils.get_srp_pool_stats(self.conn, arrayName, poolName))

        LOG.info(_LI(
            "Capacity stats for SRP pool %(poolName)s on array "
            "%(arrayName)s (total_capacity_gb=%(total_capacity_gb)lu, "
            "free_capacity_gb=%(free_capacity_gb)lu"),
            {'poolName': poolName,
             'arrayName': arrayName,
             'total_capacity_gb': totalManagedSpaceGbs,
             'free_capacity_gb': remainingManagedSpaceGbs})
        slo = self.utils.parse_slo_from_file(emcConfigFileName)
        workload = self.utils.parse_workload_from_file(emcConfigFileName)

        location_info = ("%(arrayName)s#%(poolName)s#%(slo)s#%(workload)s"
                         % {'arrayName': arrayName,
                            'poolName': poolName,
                            'slo': slo,
                            'workload': workload})

        return location_info, totalManagedSpaceGbs, remainingManagedSpaceGbs

    def retype(self, ctxt, volume, new_type, diff, host):
        """Migrate volume to another host using retype.

        :param ctxt: context
        :param volume: the volume object including the volume_type_id
        :param new_type: the new volume type.
        :param host: The host dict holding the relevant target(destination)
               information
        :returns: boolean True/False
        :returns: list
        """

        volumeName = volume['name']
        volumeStatus = volume['status']
        LOG.info(_LI("Migrating using retype Volume: %(volume)s"),
                 {'volume': volumeName})

        extraSpecs = self._initial_setup(volume)
        self.conn = self._get_ecom_connection()

        volumeInstance = self._find_lun(volume)
        if volumeInstance is None:
            LOG.error(_LE("Volume %(name)s not found on the array. "
                          "No volume to migrate using retype."),
                      {'name': volumeName})
            return False

        if extraSpecs[ISV3]:
            return self._slo_workload_migration(volumeInstance, volume, host,
                                                volumeName, volumeStatus,
                                                extraSpecs, new_type)
        else:
            return self._pool_migration(volumeInstance, volume, host,
                                        volumeName, volumeStatus,
                                        extraSpecs[FASTPOLICY], new_type)

    def migrate_volume(self, ctxt, volume, host, new_type=None):
        """Migrate volume to another host

        :param ctxt: context
        :param volume: the volume object including the volume_type_id
        :param host: the host dict holding the relevant target(destination)
               information
        :param new_type: None
        :returns: boolean True/False
        :returns: list
        """
        LOG.warn(_LW("The VMAX plugin only supports Retype.  "
                     "If a pool based migration is necessary "
                     "this will happen on a Retype "
                     "From the command line: "
                     "cinder --os-volume-api-version 2 retype "
                     "<volumeId> <volumeType> --migration-policy on-demand"))
        return True, {}

    def _migrate_volume(
            self, volume, volumeInstance, targetPoolName,
            targetFastPolicyName, sourceFastPolicyName, new_type=None):
        """Migrate volume to another host

        :param volume: the volume object including the volume_type_id
        :param volumeInstance: the volume instance
        :param targetPoolName: the target poolName
        :param targetFastPolicyName: the target FAST policy name, can be None
        :param sourceFastPolicyName: the source FAST policy name, can be None
        :param new_type: None
        :returns:  boolean True/False
        :returns:  empty list
        """
        volumeName = volume['name']
        storageSystemName = volumeInstance['SystemName']

        sourcePoolInstanceName = self.utils.get_assoc_pool_from_volume(
            self.conn, volumeInstance.path)

        moved, rc = self._migrate_volume_from(
            volume, volumeInstance, targetPoolName, sourceFastPolicyName)

        if moved is False and sourceFastPolicyName is not None:
            # Return the volume to the default source fast policy storage
            # group because the migrate was unsuccessful
            LOG.warn(_LW(
                "Failed to migrate: %(volumeName)s from "
                "default source storage group "
                "for FAST policy: %(sourceFastPolicyName)s "
                "Attempting cleanup... "),
                {'volumeName': volumeName,
                 'sourceFastPolicyName': sourceFastPolicyName})
            if sourcePoolInstanceName == self.utils.get_assoc_pool_from_volume(
                    self.conn, volumeInstance.path):
                self._migrate_cleanup(self.conn, volumeInstance,
                                      storageSystemName, sourceFastPolicyName,
                                      volumeName)
            else:
                # migrate was successful but still issues
                self._migrate_rollback(
                    self.conn, volumeInstance, storageSystemName,
                    sourceFastPolicyName, volumeName, sourcePoolInstanceName)

            return moved

        if targetFastPolicyName == 'None':
            targetFastPolicyName = None

        if moved is True and targetFastPolicyName is not None:
            if not self._migrate_volume_fast_target(
                    volumeInstance, storageSystemName,
                    targetFastPolicyName, volumeName):
                LOG.warn(_LW(
                    "Attempting a rollback of: %(volumeName)s to "
                    "original pool %(sourcePoolInstanceName)s "),
                    {'volumeName': volumeName,
                     'sourcePoolInstanceName': sourcePoolInstanceName})
                self._migrate_rollback(
                    self.conn, volumeInstance, storageSystemName,
                    sourceFastPolicyName, volumeName, sourcePoolInstanceName)

        if rc == 0:
            moved = True

        return moved

    def _migrate_rollback(self, conn, volumeInstance,
                          storageSystemName, sourceFastPolicyName,
                          volumeName, sourcePoolInstanceName):
        """Full rollback

        Failed on final step on adding migrated volume to new target
        default storage group for the target FAST policy

        :param conn: connection info to ECOM
        :param volumeInstance: the volume instance
        :param storageSystemName: the storage system name
        :param sourceFastPolicyName: the source FAST policy name
        :param volumeName: the volume Name

        :returns: boolean True/False
        :returns: int, the return code from migrate operation
        """

        LOG.warn(_LW("_migrate_rollback on : %(volumeName)s from "),
                 {'volumeName': volumeName})

        storageRelocationService = self.utils.find_storage_relocation_service(
            conn, storageSystemName)

        try:
            self.provision.migrate_volume_to_storage_pool(
                conn, storageRelocationService, volumeInstance.path,
                sourcePoolInstanceName)
        except Exception:
            LOG.error(_LE(
                "Failed to return volume %(volumeName)s to "
                "original storage pool. Please contact your system "
                "administrator to return it to the correct location "),
                {'volumeName': volumeName})

        if sourceFastPolicyName is not None:
            self.add_to_default_SG(
                conn, volumeInstance, storageSystemName, sourceFastPolicyName,
                volumeName)

    def _migrate_cleanup(self, conn, volumeInstance,
                         storageSystemName, sourceFastPolicyName,
                         volumeName):
        """If the migrate fails, put volume back to source FAST SG

        :param conn: connection info to ECOM
        :param volumeInstance: the volume instance
        :param storageSystemName: the storage system name
        :param sourceFastPolicyName: the source FAST policy name
        :param volumeName: the volume Name

        :returns: boolean True/False
        :returns: int, the return code from migrate operation
        """

        LOG.warn(_LW("_migrate_cleanup on : %(volumeName)s from "),
                 {'volumeName': volumeName})

        controllerConfigurationService = (
            self.utils.find_controller_configuration_service(
                conn, storageSystemName))

        # check to see what SG it is in
        assocStorageGroupInstanceName = (
            self.utils.get_storage_group_from_volume(conn,
                                                     volumeInstance.path))
        # This is the SG it should be in
        defaultStorageGroupInstanceName = (
            self.fast.get_policy_default_storage_group(
                conn, controllerConfigurationService, sourceFastPolicyName))

        # It is not in any storage group.  Must add it to default source
        if assocStorageGroupInstanceName is None:
            self.add_to_default_SG(conn, volumeInstance,
                                   storageSystemName, sourceFastPolicyName,
                                   volumeName)

        # It is in the incorrect storage group
        if (assocStorageGroupInstanceName is not None and
                (assocStorageGroupInstanceName !=
                    defaultStorageGroupInstanceName)):
            self.provision.remove_device_from_storage_group(
                conn, controllerConfigurationService,
                assocStorageGroupInstanceName, volumeInstance.path, volumeName)

            self.add_to_default_SG(
                conn, volumeInstance, storageSystemName, sourceFastPolicyName,
                volumeName)

    def _migrate_volume_fast_target(
            self, volumeInstance, storageSystemName,
            targetFastPolicyName, volumeName):
        """If the target host is FAST enabled.

        If the target host is FAST enabled then we need to add it to the
        default storage group for that policy

        :param volumeInstance: the volume instance
        :param storageSystemName: the storage system name
        :param targetFastPolicyName: the target fast policy name
        :param volumeName: the volume name
        :returns: boolean True/False
        """
        falseRet = False
        LOG.info(_LI(
            "Adding volume: %(volumeName)s to default storage group "
            "for FAST policy: %(fastPolicyName)s "),
            {'volumeName': volumeName,
             'fastPolicyName': targetFastPolicyName})

        controllerConfigurationService = (
            self.utils.find_controller_configuration_service(
                self.conn, storageSystemName))

        defaultStorageGroupInstanceName = (
            self.fast.get_or_create_default_storage_group(
                self.conn, controllerConfigurationService,
                targetFastPolicyName, volumeInstance))
        if defaultStorageGroupInstanceName is None:
            LOG.error(_LE(
                "Unable to create or get default storage group for FAST policy"
                ": %(fastPolicyName)s. "),
                {'fastPolicyName': targetFastPolicyName})

            return falseRet

        defaultStorageGroupInstanceName = (
            self.fast.add_volume_to_default_storage_group_for_fast_policy(
                self.conn, controllerConfigurationService, volumeInstance,
                volumeName, targetFastPolicyName))
        if defaultStorageGroupInstanceName is None:
            LOG.error(_LE(
                "Failed to verify that volume was added to storage group for "
                "FAST policy: %(fastPolicyName)s. "),
                {'fastPolicyName': targetFastPolicyName})
            return falseRet

        return True

    def _migrate_volume_from(self, volume, volumeInstance,
                             targetPoolName, sourceFastPolicyName):
        """Check FAST policies and migrate from source pool

        :param volume: the volume object including the volume_type_id
        :param volumeInstance: the volume instance
        :param targetPoolName: the target poolName
        :param sourceFastPolicyName: the source FAST policy name, can be None
        :returns: boolean True/False
        :returns: int, the return code from migrate operation
        """
        falseRet = (False, -1)
        volumeName = volume['name']
        storageSystemName = volumeInstance['SystemName']

        LOG.debug("sourceFastPolicyName is : %(sourceFastPolicyName)s. ",
                  {'sourceFastPolicyName': sourceFastPolicyName})

        # If the source volume is is FAST enabled it must first be removed
        # from the default storage group for that policy
        if sourceFastPolicyName is not None:
            self.remove_from_default_SG(
                self.conn, volumeInstance, storageSystemName,
                sourceFastPolicyName, volumeName)

        # migrate from one pool to another
        storageRelocationService = self.utils.find_storage_relocation_service(
            self.conn, storageSystemName)

        targetPoolInstanceName = self.utils.get_pool_by_name(
            self.conn, targetPoolName, storageSystemName)
        if targetPoolInstanceName is None:
            LOG.error(_LE(
                "Error finding targe pool instance name for pool: "
                "%(targetPoolName)s. "),
                {'targetPoolName': targetPoolName})
            return falseRet
        try:
            rc = self.provision.migrate_volume_to_storage_pool(
                self.conn, storageRelocationService, volumeInstance.path,
                targetPoolInstanceName)
        except Exception as e:
            # rollback by deleting the volume if adding the volume to the
            # default storage group were to fail
            LOG.error(_LE("Exception: %s"), e)
            LOG.error(_LE(
                "Error migrating volume: %(volumename)s. "
                "to target pool  %(targetPoolName)s. "),
                {'volumename': volumeName,
                 'targetPoolName': targetPoolName})
            return falseRet

        # check that the volume is now migrated to the correct storage pool,
        # if it is terminate the migrate session
        foundPoolInstanceName = self.utils.get_assoc_pool_from_volume(
            self.conn, volumeInstance.path)

        if (foundPoolInstanceName is None or
                (foundPoolInstanceName['InstanceID'] !=
                    targetPoolInstanceName['InstanceID'])):
            LOG.error(_LE(
                "Volume : %(volumeName)s. was not successfully migrated to "
                "target pool %(targetPoolName)s."),
                {'volumeName': volumeName,
                 'targetPoolName': targetPoolName})
            return falseRet

        else:
            LOG.debug("Terminating migration session on : %(volumeName)s. ",
                      {'volumeName': volumeName})
            self.provision._terminate_migrate_session(
                self.conn, volumeInstance.path)

        if rc == 0:
            moved = True

        return moved, rc

    def remove_from_default_SG(
            self, conn, volumeInstance, storageSystemName,
            sourceFastPolicyName, volumeName):
        """For FAST, remove volume from default storage group

        :param conn: connection info to ECOM
        :param volumeInstance: the volume instance
        :param storageSystemName: the storage system name
        :param sourceFastPolicyName: the source FAST policy name
        :param volumeName: the volume Name

        :returns: boolean True/False
        :returns: int, the return code from migrate operation
        """
        controllerConfigurationService = (
            self.utils.find_controller_configuration_service(
                conn, storageSystemName))
        try:
            defaultStorageGroupInstanceName = (
                self.masking.remove_device_from_default_storage_group(
                    conn, controllerConfigurationService,
                    volumeInstance.path, volumeName, sourceFastPolicyName))
        except Exception as ex:
            LOG.error(_LE("Exception: %s"), ex)
            exceptionMessage = (_(
                "Failed to remove: %(volumename)s. "
                "from the default storage group for "
                "FAST policy %(fastPolicyName)s. ")
                % {'volumename': volumeName,
                   'fastPolicyName': sourceFastPolicyName})

            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        if defaultStorageGroupInstanceName is None:
            LOG.warn(_LW(
                "The volume: %(volumename)s. "
                "was not first part of the default storage "
                "group for FAST policy %(fastPolicyName)s."),
                {'volumename': volumeName,
                 'fastPolicyName': sourceFastPolicyName})

    def add_to_default_SG(
            self, conn, volumeInstance, storageSystemName,
            targetFastPolicyName, volumeName):
        """For FAST, add volume to default storage group

        :param conn: connection info to ECOM
        :param volumeInstance: the volume instance
        :param storageSystemName: the storage system name
        :param targetFastPolicyName: the target FAST policy name
        :param volumeName: the volume Name

        :returns: boolean True/False
        :returns: int, the return code from migrate operation
        """
        controllerConfigurationService = (
            self.utils.find_controller_configuration_service(
                conn, storageSystemName))
        assocDefaultStorageGroupName = (
            self.fast
            .add_volume_to_default_storage_group_for_fast_policy(
                conn, controllerConfigurationService, volumeInstance,
                volumeName, targetFastPolicyName))
        if assocDefaultStorageGroupName is None:
            LOG.error(_LE(
                "Failed to add %(volumeName)s "
                "to default storage group for fast policy "
                "%(fastPolicyName)s "),
                {'volumeName': volumeName,
                 'fastPolicyName': targetFastPolicyName})

    def _is_valid_for_storage_assisted_migration_v3(
            self, volumeInstanceName, host, sourceArraySerialNumber,
            sourcePoolName, volumeName, volumeStatus):
        """Check if volume is suitable for storage assisted (pool) migration.

        :param volumeInstanceName: the volume instance id
        :param host: the host object
        :param sourceArraySerialNumber: the array serial number of
                                  the original volume
        :param sourcePoolName: the pool name
                                  the original volume
        :param volumeName: the name of the volume to be migrated
        :param volumeStatus: the status of the volume e.g
        :returns: boolean, True/False
        :returns: string, targetSlo
        :returns: string, targetWorkload
        """
        falseRet = (False, None, None)
        if 'location_info' not in host['capabilities']:
            LOG.error(_LE('Error getting array, pool, SLO and workload'))
            return falseRet
        info = host['capabilities']['location_info']

        LOG.debug("Location info is : %(info)s.",
                  {'info': info})
        try:
            infoDetail = info.split('#')
            targetArraySerialNumber = infoDetail[0]
            targetPoolName = infoDetail[1]
            targetSlo = infoDetail[2]
            targetWorkload = infoDetail[3]
        except Exception:
            LOG.error(_LE("Error parsing array, pool, SLO and workload"))

        if targetArraySerialNumber not in sourceArraySerialNumber:
            LOG.error(_LE(
                "The source array : %(sourceArraySerialNumber)s does not "
                "match the target array: %(targetArraySerialNumber)s"
                "skipping storage-assisted migration"),
                {'sourceArraySerialNumber': sourceArraySerialNumber,
                 'targetArraySerialNumber': targetArraySerialNumber})
            return falseRet

        if targetPoolName not in sourcePoolName:
            LOG.error(_LE(
                "Only SLO/workload migration within the same SRP Pool "
                "Is supported in this version "
                "The source pool : %(sourcePoolName)s does not "
                "match the target array: %(targetPoolName)s"
                "skipping storage-assisted migration"),
                {'sourcePoolName': sourcePoolName,
                 'targetPoolName': targetPoolName})
            return falseRet

        foundStorageGroupInstanceName = (
            self.utils.get_storage_group_from_volume(
                self.conn, volumeInstanceName))
        if foundStorageGroupInstanceName is None:
            LOG.warn(_LW(
                "Volume : %(volumeName)s is not currently "
                "belonging to any storage group "),
                {'volumeName': volumeName})

        else:
            storageGroupInstance = self.conn.GetInstance(
                foundStorageGroupInstanceName)
            emcFastSetting = self.utils._get_fast_settings_from_storage_group(
                storageGroupInstance)
            targetCombination = targetSlo + '+' + targetWorkload
            if targetCombination in emcFastSetting:
                LOG.error(_LE(
                    "No action required. Volume : %(volumeName)s is "
                    "already part of slo/workload combination : "
                    "%(targetCombination)s"),
                    {'volumeName': volumeName,
                     'targetCombination': targetCombination})
                return falseRet

        return (True, targetSlo, targetWorkload)

    def _is_valid_for_storage_assisted_migration(
            self, volumeInstanceName, host, sourceArraySerialNumber,
            volumeName, volumeStatus):
        """Check if volume is suitable for storage assisted (pool) migration.

        :param volumeInstanceName: the volume instance id
        :param host: the host object
        :param sourceArraySerialNumber: the array serial number of
                                  the original volume
        :param volumeName: the name of the volume to be migrated
        :param volumeStatus: the status of the volume e.g
        :returns: boolean, True/False
        :returns: string, targetPool
        :returns: string, targetFastPolicy
        """
        falseRet = (False, None, None)
        if 'location_info' not in host['capabilities']:
            LOG.error(_LE('Error getting target pool name and array'))
            return falseRet
        info = host['capabilities']['location_info']

        LOG.debug("Location info is : %(info)s.",
                  {'info': info})
        try:
            infoDetail = info.split('#')
            targetArraySerialNumber = infoDetail[0]
            targetPoolName = infoDetail[1]
            targetFastPolicy = infoDetail[2]
        except Exception:
            LOG.error(_LE(
                "Error parsing target pool name, array, and fast policy"))

        if targetArraySerialNumber not in sourceArraySerialNumber:
            LOG.error(_LE(
                "The source array : %(sourceArraySerialNumber)s does not "
                "match the target array: %(targetArraySerialNumber)s"
                "skipping storage-assisted migration"),
                {'sourceArraySerialNumber': sourceArraySerialNumber,
                 'targetArraySerialNumber': targetArraySerialNumber})
            return falseRet

        # get the pool from the source array and check that is is different
        # to the pool in the target array
        assocPoolInstanceName = self.utils.get_assoc_pool_from_volume(
            self.conn, volumeInstanceName)
        assocPoolInstance = self.conn.GetInstance(
            assocPoolInstanceName)
        if assocPoolInstance['ElementName'] == targetPoolName:
            LOG.error(_LE(
                "No action required. Volume : %(volumeName)s is "
                "already part of pool : %(pool)s"),
                {'volumeName': volumeName,
                 'pool': targetPoolName})
            return falseRet

        LOG.info(_LI("Volume status is: %s"), volumeStatus)
        if (host['capabilities']['storage_protocol'] != self.protocol and
                (volumeStatus != 'available' and volumeStatus != 'retyping')):
            LOG.error(_LE(
                "Only available volumes can be migrated between "
                "different protocols"))
            return falseRet

        return (True, targetPoolName, targetFastPolicy)

    def _set_config_file_and_get_extra_specs(self, volume, volumeTypeId=None):
        """Given the volume object get the associated volumetype.

        Given the volume object get the associated volumetype and the
        extra specs associated with it.
        Based on the name of the config group, register the config file

        :param volume: the volume object including the volume_type_id
        :returns: tuple the extra specs tuple
        :returns: string configuration file
        """
        extraSpecs = self.utils.get_volumetype_extraspecs(volume, volumeTypeId)
        configGroup = None

        # If there are no extra specs then the default case is assumed
        if extraSpecs:
            configGroup = self.configuration.config_group

        configurationFile = self._register_config_file_from_config_group(
            configGroup)

        return extraSpecs, configurationFile

    def _get_ecom_connection(self):
        """Get the ecom connection

        :returns: conn,the ecom connection
        """
        if self.ecomUseSSL:
            pywbem.cim_http.wbem_request = emc_vmax_https.wbem_request
            if updatedPywbem:
                conn = pywbem.WBEMConnection(
                    self.url,
                    (self.user, self.passwd),
                    default_namespace='root/emc',
                    x509={"key_file":
                          self.configuration.safe_get(
                              'driver_client_cert_key'),
                          "cert_file":
                          self.configuration.safe_get('driver_client_cert')},
                    ca_certs=self.ecomCACert,
                    no_verification=self.ecomNoVerification)
            else:
                conn = pywbem.WBEMConnection(
                    self.url,
                    (self.user, self.passwd),
                    default_namespace='root/emc',
                    x509={"key_file":
                          self.configuration.safe_get(
                              'driver_client_cert_key'),
                          "cert_file":
                          self.configuration.safe_get('driver_client_cert')})

        else:
            conn = pywbem.WBEMConnection(
                self.url,
                (self.user, self.passwd),
                default_namespace='root/emc')

        conn.debug = True
        if conn is None:
            exception_message = (_("Cannot connect to ECOM server"))
            raise exception.VolumeBackendAPIException(data=exception_message)

        return conn

    def _find_pool_in_array(self, arrayStr, poolNameInStr, isV3):
        """Find a pool based on the pool name on a given array.

        :param arrayStr: the array Serial number (String)
        :param poolNameInStr: the name of the poolname (String)
        :param isv3: True/False
        :returns: foundPoolInstanceName, the CIM Instance Name of the Pool
        """
        foundPoolInstanceName = None
        systemNameStr = None

        storageSystemInstanceName = self.utils.find_storageSystem(
            self.conn, arrayStr)

        if isV3:
            foundPoolInstanceName, systemNameStr = (
                self.utils.get_pool_and_system_name_v3(
                    self.conn, storageSystemInstanceName, poolNameInStr))
        else:
            foundPoolInstanceName, systemNameStr = (
                self.utils.get_pool_and_system_name_v2(
                    self.conn, storageSystemInstanceName, poolNameInStr))

        if foundPoolInstanceName is None:
            exceptionMessage = (_("Pool %(poolNameInStr)s is not found.")
                                % {'poolNameInStr': poolNameInStr})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        if systemNameStr is None:
            exception_message = (_("Storage system not found for pool "
                                   "%(poolNameInStr)s.")
                                 % {'poolNameInStr': poolNameInStr})
            LOG.error(exception_message)
            raise exception.VolumeBackendAPIException(data=exception_message)

        LOG.debug("Pool: %(pool)s  SystemName: %(systemname)s.",
                  {'pool': foundPoolInstanceName,
                   'systemname': systemNameStr})
        return foundPoolInstanceName, systemNameStr

    def _find_lun(self, volume):
        """Given the volume get the instance from it.

        :param conn: connection the the ecom server
        :param volume: volume object
        :returns: foundVolumeinstance
        """
        foundVolumeinstance = None
        volumename = volume['name']

        loc = volume['provider_location']
        if self.conn is None:
            self.conn = self._get_ecom_connection()

        if isinstance(loc, six.string_types):
            name = eval(loc)

            instancename = self.utils.get_instance_name(
                name['classname'], name['keybindings'])

            # Handle the case where volume cannot be found.
            foundVolumeinstance = self.utils.get_existing_instance(
                self.conn, instancename)

        if foundVolumeinstance is None:
            LOG.debug("Volume %(volumename)s not found on the array.",
                      {'volumename': volumename})
        else:
            LOG.debug("Volume name: %(volumename)s  Volume instance: "
                      "%(foundVolumeinstance)s.",
                      {'volumename': volumename,
                       'foundVolumeinstance': foundVolumeinstance})

        return foundVolumeinstance

    def _find_storage_sync_sv_sv(self, snapshot, volume,
                                 waitforsync=True):
        """Find the storage synchronized name

        :param snapshot: snapshot object
        :param volume: volume object
        :returns: foundsyncname (String)
        :returns: storage_system (String)
        """
        snapshotname = snapshot['name']
        volumename = volume['name']
        LOG.debug("Source: %(volumename)s  Target: %(snapshotname)s.",
                  {'volumename': volumename, 'snapshotname': snapshotname})

        snapshot_instance = self._find_lun(snapshot)
        volume_instance = self._find_lun(volume)
        storage_system = volume_instance['SystemName']
        classname = 'SE_StorageSynchronized_SV_SV'
        bindings = {'SyncedElement': snapshot_instance.path,
                    'SystemElement': volume_instance.path}
        foundsyncname = self.utils.get_instance_name(classname, bindings)

        if foundsyncname is None:
            LOG.debug(
                "Source: %(volumename)s  Target: %(snapshotname)s. "
                "Storage Synchronized not found. ",
                {'volumename': volumename,
                 'snapshotname': snapshotname})
        else:
            LOG.debug("Storage system: %(storage_system)s  "
                      "Storage Synchronized instance: %(sync)s.",
                      {'storage_system': storage_system,
                       'sync': foundsyncname})
            # Wait for SE_StorageSynchronized_SV_SV to be fully synced
            if waitforsync:
                self.utils.wait_for_sync(self.conn, foundsyncname)

        return foundsyncname, storage_system

    def _find_initiator_names(self, connector):
        foundinitiatornames = []
        iscsi = 'iscsi'
        fc = 'fc'
        name = 'initiator name'
        if self.protocol.lower() == iscsi and connector['initiator']:
            foundinitiatornames.append(connector['initiator'])
        elif self.protocol.lower() == fc and connector['wwpns']:
            for wwn in connector['wwpns']:
                foundinitiatornames.append(wwn)
            name = 'world wide port names'

        if foundinitiatornames is None or len(foundinitiatornames) == 0:
            msg = (_("Error finding %s."), name)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        LOG.debug("Found %(name)s: %(initiator)s.",
                  {'name': name,
                   'initiator': foundinitiatornames})
        return foundinitiatornames

    def find_device_number(self, volume, connector):
        """Given the volume dict find a device number.

        Find a device number  that a host can see
        for a volume

        :param volume: the volume dict
        :param connector: the connector dict
        :returns: data, the data dict

        """
        foundNumDeviceNumber = None
        foundMaskingViewName = None
        volumeName = volume['name']
        volumeInstance = self._find_lun(volume)
        storageSystemName = volumeInstance['SystemName']

        unitnames = self.conn.ReferenceNames(
            volumeInstance.path,
            ResultClass='CIM_ProtocolControllerForUnit')

        for unitname in unitnames:
            controller = unitname['Antecedent']
            classname = controller['CreationClassName']
            index = classname.find('Symm_LunMaskingView')
            if index > -1:
                unitinstance = self.conn.GetInstance(unitname,
                                                     LocalOnly=False)
                numDeviceNumber = int(unitinstance['DeviceNumber'],
                                      16)
                foundNumDeviceNumber = numDeviceNumber
                controllerInstance = self.conn.GetInstance(controller,
                                                           LocalOnly=False)
                propertiesList = controllerInstance.properties.items()
                for properties in propertiesList:
                    if properties[0] == 'ElementName':
                        cimProperties = properties[1]
                        foundMaskingViewName = cimProperties.value
                        break

                break

        if foundNumDeviceNumber is None:
            LOG.debug(
                "Device number not found for volume "
                "%(volumeName)s %(volumeInstance)s.",
                {'volumeName': volumeName,
                 'volumeInstance': volumeInstance.path})

        data = {'hostlunid': foundNumDeviceNumber,
                'storagesystem': storageSystemName,
                'maskingview': foundMaskingViewName}

        LOG.debug("Device info: %(data)s.", {'data': data})

        return data

    def get_target_wwns(self, storageSystem, connector):
        """Find target WWNs.

        :param storageSystem: the storage system name
        :param connector: the connector dict
        :returns: targetWwns, the target WWN list
        """
        targetWwns = []

        storageHardwareService = self.utils.find_storage_hardwareid_service(
            self.conn, storageSystem)

        hardwareIdInstances = self._find_storage_hardwareids(
            connector, storageHardwareService)

        LOG.debug(
            "EMCGetTargetEndpoints: Service: %(service)s  "
            "Storage HardwareIDs: %(hardwareIds)s.",
            {'service': storageHardwareService,
             'hardwareIds': hardwareIdInstances})

        for hardwareIdInstance in hardwareIdInstances:
            LOG.debug("HardwareID instance is  : %(hardwareIdInstance)s  ",
                      {'hardwareIdInstance': hardwareIdInstance})
            try:
                rc, targetEndpoints = (  # @UnusedVariable
                    self.provision.get_target_endpoints(
                        self.conn, storageHardwareService, hardwareIdInstance))
            except Exception as ex:
                LOG.error(_LE("Exception: %s"), ex)
                errorMessage = (_(
                    "Unable to get target endpoints for hardwareId "
                    "%(hardwareIdInstance)s")
                    % {'hardwareIdInstance': hardwareIdInstance})
                LOG.error(errorMessage)
                raise exception.VolumeBackendAPIException(data=errorMessage)

            if targetEndpoints:
                endpoints = targetEndpoints['TargetEndpoints']

                LOG.debug("There are  %(len)lu endpoints ",
                          {'len': len(endpoints)})
                for targetendpoint in endpoints:
                    wwn = targetendpoint['Name']
                    # Add target wwn to the list if it is not already there
                    if not any(d == wwn for d in targetWwns):
                        targetWwns.append(wwn)
            else:
                LOG.error(_LE(
                    "Target end points do not exist for hardware Id : "
                    "%(hardwareIdInstance)s "),
                    {'hardwareIdInstance': hardwareIdInstance})

        LOG.debug("Target WWNs: : %(targetWwns)s  ",
                  {'targetWwns': targetWwns})

        return targetWwns

    def _find_storage_hardwareids(
            self, connector, hardwareIdManagementService):
        """Find the storage hardware ID instances.

        :param connector: the connector dict
        :param hardwareIdManagementService: the storage Hardware
                                            management service
        :returns: foundInstances, the list of storage hardware ID instances
        """
        foundHardwareIdList = []
        wwpns = self._find_initiator_names(connector)

        hardwareIdInstances = (
            self.utils.get_hardware_id_instances_from_array(
                self.conn, hardwareIdManagementService))
        for hardwareIdInstance in hardwareIdInstances:
            storageId = hardwareIdInstance['StorageID']
            for wwpn in wwpns:
                if wwpn.lower() == storageId.lower():
                    # Check that the found hardwareId has not been
                    # deleted. If it has, we don't want to add it to the list.
                    instance = self.utils.get_existing_instance(
                        self.conn, hardwareIdInstance.path)
                    if instance is None:
                        # hardwareId doesn't exist any more. Skip it.
                        break
                    foundHardwareIdList.append(hardwareIdInstance.path)
                    break

        LOG.debug("Storage Hardware IDs for %(wwpns)s is "
                  "%(foundInstances)s.",
                  {'wwpns': wwpns,
                   'foundInstances': foundHardwareIdList})

        return foundHardwareIdList

    def _register_config_file_from_config_group(self, configGroupName):
        """Given the config group name register the file.

        :param configGroupName: the config group name
        :returns: string configurationFile
        """
        if configGroupName is None:
            self._set_ecom_credentials(CINDER_EMC_CONFIG_FILE)
            return CINDER_EMC_CONFIG_FILE
        if hasattr(self.configuration, 'cinder_emc_config_file'):
            configurationFile = self.configuration.cinder_emc_config_file
        else:
            configurationFile = (
                CINDER_EMC_CONFIG_FILE_PREFIX + configGroupName +
                CINDER_EMC_CONFIG_FILE_POSTFIX)

        # The file saved in self.configuration may not be the correct one,
        # double check
        if configGroupName not in configurationFile:
            configurationFile = (
                CINDER_EMC_CONFIG_FILE_PREFIX + configGroupName +
                CINDER_EMC_CONFIG_FILE_POSTFIX)

        self._set_ecom_credentials(configurationFile)
        return configurationFile

    def _set_ecom_credentials(self, configurationFile):
        """Given the configuration file set the ecom credentials.

        :param configurationFile: name of the file (String)
        :raises: VolumeBackendAPIException
        """
        if os.path.isfile(configurationFile):
            LOG.debug("Configuration file : %(configurationFile)s exists",
                      {'configurationFile': configurationFile})
        else:
            exceptionMessage = (_(
                "Configuration file %(configurationFile)s does not exist ")
                % {'configurationFile': configurationFile})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        ip, port = self.utils.get_ecom_server(configurationFile)
        self.user, self.passwd = self.utils.get_ecom_cred(configurationFile)
        self.ecomUseSSL, self.ecomCACert, self.ecomNoVerification = (
            self.utils.get_ecom_cred_SSL(configurationFile))
        if self.ecomUseSSL:
            self.url = 'https://' + ip + ':' + port
        else:
            self.url = 'http://' + ip + ':' + port
        self.conn = self._get_ecom_connection()

    def _initial_setup(self, volume, volumeTypeId=None):
        """Necessary setup to accumulate the relevant information.

        The volume object has a host in which we can parse the
        config group name. The config group name is the key to our EMC
        configuration file. The emc configuration file contains pool name
        and array name which are mandatory fields.
        FastPolicy is optional.
        StripedMetaCount is an extra spec that determines whether
        the composite volume should be concatenated or striped.

        :param volume: the volume Object
        :returns: tuple extra spec tuple
        :returns: string the configuration file
        """
        try:
            extraSpecs, configurationFile = (
                self._set_config_file_and_get_extra_specs(
                    volume, volumeTypeId))

            arrayName = self.utils.parse_array_name_from_file(
                configurationFile)
            if arrayName is None:
                exceptionMessage = (_(
                    "The array cannot be null. The pool must be configured "
                    "either as a cinder extra spec for multi-backend or in "
                    "the EMC configuration file for the default case "))
                LOG.error(exceptionMessage)
                raise exception.VolumeBackendAPIException(
                    data=exceptionMessage)

            isV3 = self.utils.isArrayV3(self.conn, arrayName)

            if isV3:
                extraSpecs = self._set_v3_extra_specs(
                    extraSpecs, configurationFile, arrayName)
            else:
                # V2 extra specs
                extraSpecs = self._set_v2_extra_specs(
                    extraSpecs, configurationFile, arrayName)
        except Exception:
            exceptionMessage = (_(
                "Unable to get configuration information necessary to create "
                "a volume. Please check that there is a configuration file "
                "for each config group, if multi-backend is enabled. "
                "The should be in the following format "
                "/etc/cinder/cinder_emc_config_<CONFIG_GROUP>.xml"))
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        return extraSpecs

    def _get_pool_and_storage_system(self, extraSpecs):
        """Given the extra specs get the pool and storage system name.

        :params extraSpecs: the extra spec tuple
        :returns: poolInstanceName The pool instance name
        :returns: String  the storage system name
        """

        try:
            array = extraSpecs[ARRAY]
            poolInstanceName, storageSystemStr = self._find_pool_in_array(
                array, extraSpecs[POOL], extraSpecs[ISV3])
        except Exception:
            exceptionMessage = (_(
                "You must supply an array in your EMC configuration file "))
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        if poolInstanceName is None or storageSystemStr is None:
            exceptionMessage = (_(
                "Cannot get necessary pool or storage system information "))
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        return poolInstanceName, storageSystemStr

    def _populate_masking_dict(self, volume, connector, extraSpecs):
        """Get all the names of the maskingView and subComponents.

        :param volume: the volume object
        :param connector: the connector object
        :param extraSpecs: the extra spec tuple
        :returns: tuple maskingViewDict a tuple with masking view information
        """
        maskingViewDict = {}
        hostName = connector['host']
        poolName = extraSpecs[POOL]
        isV3 = extraSpecs[ISV3]
        maskingViewDict['isV3'] = isV3
        protocol = self.utils.get_short_protocol_type(self.protocol)
        shortHostName = self.utils.get_host_short_name(hostName)
        if isV3:
            slo = extraSpecs[SLO]
            workload = extraSpecs[WORKLOAD]
            maskingViewDict['slo'] = slo
            maskingViewDict['workload'] = workload
            maskingViewDict['pool'] = poolName
            maskingViewDict['sgGroupName'] = (
                'OS-' + shortHostName + '-' + poolName + '-' + slo + '-' +
                workload + '-SG')
            maskingViewDict['maskingViewName'] = (
                'OS-' + shortHostName + '-' + poolName + '-' + slo + '-' +
                workload + '-MV')
        else:
            maskingViewDict['sgGroupName'] = (
                'OS-' + shortHostName + '-' + poolName + '-' +
                protocol + '-SG')
            maskingViewDict['maskingViewName'] = (
                'OS-' + shortHostName + '-' + poolName + '-' +
                protocol + '-MV')
            maskingViewDict['fastPolicy'] = (
                self.utils.parse_fast_policy_name_from_file(
                    self.configuration.cinder_emc_config_file))

        volumeName = volume['name']
        volumeInstance = self._find_lun(volume)
        storageSystemName = volumeInstance['SystemName']

        maskingViewDict['controllerConfigService'] = (
            self.utils.find_controller_configuration_service(
                self.conn, storageSystemName))
        # The portGroup is gotten from emc xml config file
        maskingViewDict['pgGroupName'] = (
            self.utils.parse_file_to_get_port_group_name(
                self.configuration.cinder_emc_config_file))

        maskingViewDict['igGroupName'] = (
            'OS-' + shortHostName + '-' + protocol + '-IG')
        maskingViewDict['connector'] = connector
        maskingViewDict['volumeInstance'] = volumeInstance
        maskingViewDict['volumeName'] = volumeName
        maskingViewDict['storageSystemName'] = storageSystemName

        return maskingViewDict

    def _add_volume_to_default_storage_group_on_create(
            self, volumeDict, volumeName, storageConfigService,
            storageSystemName, fastPolicyName):
        """Add the volume to the default storage group for that policy.

        On a create when fast policy is enable add the volume to the default
        storage group for that policy. If it fails do the necessary rollback

        :param volumeDict: the volume dictionary
        :param volumeName: the volume name (String)
        :param storageConfigService: the storage configuration service
        :param storageSystemName: the storage system name (String)
        :param fastPolicyName: the fast policy name (String)
        :returns: tuple maskingViewDict with masking view information
        """
        try:
            volumeInstance = self.utils.find_volume_instance(
                self.conn, volumeDict, volumeName)
            controllerConfigurationService = (
                self.utils.find_controller_configuration_service(
                    self.conn, storageSystemName))

            self.fast.add_volume_to_default_storage_group_for_fast_policy(
                self.conn, controllerConfigurationService, volumeInstance,
                volumeName, fastPolicyName)
            foundStorageGroupInstanceName = (
                self.utils.get_storage_group_from_volume(
                    self.conn, volumeInstance.path))

            if foundStorageGroupInstanceName is None:
                exceptionMessage = (_(
                    "Error adding Volume: %(volumeName)s.  "
                    "with instance path: %(volumeInstancePath)s. ")
                    % {'volumeName': volumeName,
                       'volumeInstancePath': volumeInstance.path})
                LOG.error(exceptionMessage)
                raise exception.VolumeBackendAPIException(
                    data=exceptionMessage)
        except Exception as e:
            # rollback by deleting the volume if adding the volume to the
            # default storage group were to fail
            LOG.error(_LE("Exception: %s"), e)
            errorMessage = (_(
                "Rolling back %(volumeName)s by deleting it. ")
                % {'volumeName': volumeName})
            LOG.error(errorMessage)
            self.provision.delete_volume_from_pool(
                self.conn, storageConfigService, volumeInstance.path,
                volumeName)
            raise exception.VolumeBackendAPIException(data=errorMessage)

    def _create_and_get_unbound_volume(
            self, conn, storageConfigService, compositeVolumeInstanceName,
            additionalSize):
        """Create an unbound volume.

        Create an unbound volume so it is in the correct state to add to a
        composite volume

        :param conn: the connection information to the ecom server
        :param storageConfigService: the storage config service instance name
        :param compositeVolumeInstanceName: the composite volume instance name
        :param additionalSize: the size you want to increase the volume by
        :returns: volume instance modifiedCompositeVolumeInstance
        """
        assocPoolInstanceName = self.utils.get_assoc_pool_from_volume(
            conn, compositeVolumeInstanceName)
        appendVolumeInstance = self._create_and_get_volume_instance(
            conn, storageConfigService, assocPoolInstanceName, 'appendVolume',
            additionalSize)
        isVolumeBound = self.utils.is_volume_bound_to_pool(
            conn, appendVolumeInstance)

        if 'True' in isVolumeBound:
            appendVolumeInstance = (
                self._unbind_and_get_volume_from_storage_pool(
                    conn, storageConfigService, assocPoolInstanceName,
                    appendVolumeInstance.path, 'appendVolume'))

        return appendVolumeInstance

    def _create_and_get_volume_instance(
            self, conn, storageConfigService, poolInstanceName,
            volumeName, volumeSize):
        """Create and get a new volume.

        :params conn: the connection information to the ecom server
        :params storageConfigService: the storage config service instance name
        :params poolInstanceName: the pool instance name
        :params volumeName: the volume name
        :params volumeSize: the size to create the volume
        :returns: volumeInstance the volume instance
        """
        volumeDict, rc = (  # @UnusedVariable
            self.provision.create_volume_from_pool(
                self.conn, storageConfigService, volumeName, poolInstanceName,
                volumeSize))
        volumeInstance = self.utils.find_volume_instance(
            self.conn, volumeDict, volumeName)
        return volumeInstance

    def _unbind_and_get_volume_from_storage_pool(
            self, conn, storageConfigService, poolInstanceName,
            volumeInstanceName, volumeName):
        """Unbind a volume from a pool and return the unbound volume.

        :param conn: the connection information to the ecom server
        :param storageConfigService: the storage config service instance name
        :param poolInstanceName: the pool instance name
        :param volumeInstanceName: the volume instance name
        :param volumeName: string the volumeName
        :returns: unboundVolumeInstance the unbound volume instance
        """

        rc, job = (  # @UnusedVariable
            self.provision.unbind_volume_from_storage_pool(
                conn, storageConfigService, poolInstanceName,
                volumeInstanceName,
                volumeName))
        volumeDict = self.provision.get_volume_dict_from_job(conn, job['Job'])
        volumeInstance = self.utils.find_volume_instance(
            self.conn, volumeDict, volumeName)
        return volumeInstance

    def _modify_and_get_composite_volume_instance(
            self, conn, elementCompositionServiceInstanceName, volumeInstance,
            appendVolumeInstanceName, volumeName, compositeType):
        """Given an existing composite volume add a new composite volume to it.

        :param conn: the connection information to the ecom server
        :param elementCompositionServiceInstanceName: the storage element
                                                      composition service
                                                      instance name
        :param volumeInstanceName: the volume instance name
        :param appendVolumeInstanceName: the appended volume instance name
        :param volumeName: the volume name
        :param compositeType: concatenated
        :returns: int rc the return code
        :returns: modifiedVolumeDict the modified volume Dict
        """
        isComposite = self.utils.check_if_volume_is_composite(
            self.conn, volumeInstance)
        if 'True' in isComposite:
            rc, job = self.provision.modify_composite_volume(
                conn, elementCompositionServiceInstanceName,
                volumeInstance.path, appendVolumeInstanceName)
        elif 'False' in isComposite:
            rc, job = self.provision.create_new_composite_volume(
                conn, elementCompositionServiceInstanceName,
                volumeInstance.path, appendVolumeInstanceName, compositeType)
        else:
            LOG.error(_LE(
                "Unable to determine whether %(volumeName)s is "
                "composite or not "),
                {'volumeName': volumeName})
            raise

        modifiedVolumeDict = self.provision.get_volume_dict_from_job(
            conn, job['Job'])

        return rc, modifiedVolumeDict

    def _get_or_create_default_storage_group(
            self, conn, storageSystemName, volumeDict, volumeName,
            fastPolicyName):
        """Get or create a default storage group for a fast policy.

        :param conn: the connection information to the ecom server
        :param storageSystemName: the storage system name
        :param volumeDict: the volume dictionary
        :param volumeName: the volume name
        :param fastPolicyName: the fast policy name
        :returns: defaultStorageGroupInstanceName
        """
        controllerConfigService = (
            self.utils.find_controller_configuration_service(
                self.conn, storageSystemName))

        volumeInstance = self.utils.find_volume_instance(
            self.conn, volumeDict, volumeName)
        defaultStorageGroupInstanceName = (
            self.fast.get_or_create_default_storage_group(
                self.conn, controllerConfigService, fastPolicyName,
                volumeInstance))
        return defaultStorageGroupInstanceName

    def _create_cloned_volume(
            self, cloneVolume, sourceVolume, isSnapshot=False):
        """Create a clone volume from the source volume.

        :param cloneVolume: clone volume
        :param sourceVolume: source of the clone volume
        :returns: cloneDict the cloned volume dictionary
        """
        extraSpecs = self._initial_setup(cloneVolume)

        sourceName = sourceVolume['name']
        cloneName = cloneVolume['name']

        LOG.info(_LI(
            "Create a replica from Volume: Clone Volume: %(cloneName)s "
            " Source Volume: %(sourceName)s"),
            {'cloneName': cloneName,
             'sourceName': sourceName})

        self.conn = self._get_ecom_connection()

        sourceInstance = self._find_lun(sourceVolume)
        storageSystem = sourceInstance['SystemName']

        repServiceInstanceName = self.utils.find_replication_service(
            self.conn, storageSystem)

        LOG.debug("Create volume replica: Volume: %(cloneName)s  "
                  "Source Volume: %(sourceName)s  "
                  "Method: CreateElementReplica  "
                  "ReplicationService: %(service)s  ElementName: "
                  "%(elementname)s  SyncType: 8  SourceElement: "
                  "%(sourceelement)s",
                  {'cloneName': cloneName,
                   'sourceName': sourceName,
                   'service': repServiceInstanceName,
                   'elementname': cloneName,
                   'sourceelement': sourceInstance.path})

        if extraSpecs[ISV3]:
            rc, cloneDict = self._create_replica_v3(repServiceInstanceName,
                                                    cloneVolume,
                                                    sourceVolume,
                                                    sourceInstance,
                                                    isSnapshot)
        else:
            rc, cloneDict = self._create_clone_v2(repServiceInstanceName,
                                                  cloneVolume,
                                                  sourceVolume,
                                                  sourceInstance,
                                                  isSnapshot,
                                                  extraSpecs)
        LOG.debug("Leaving _create_cloned_volume: Volume: "
                  "%(cloneName)s Source Volume: %(sourceName)s  "
                  "Return code: %(rc)lu.",
                  {'cloneName': cloneName,
                   'sourceName': sourceName,
                   'rc': rc})

        return cloneDict

    def _add_clone_to_default_storage_group(
            self, fastPolicyName, storageSystemName, cloneDict, cloneName):
        """Helper function to add clone to the default storage group.

        :param fastPolicyName: the fast policy name
        :param storageSystemName: the storage system name
        :param cloneDict: clone dictionary
        :param cloneName: clone name
        """
        # check if the clone/snapshot volume already part of the default sg
        cloneInstance = self.utils.find_volume_instance(
            self.conn, cloneDict, cloneName)
        if self.fast.is_volume_in_default_SG(self.conn, cloneInstance.path):
            return

        # if FAST enabled place clone volume or volume from snapshot to
        # default storage group
        LOG.debug("Adding volume: %(cloneName)s to default storage group "
                  "for FAST policy: %(fastPolicyName)s ",
                  {'cloneName': cloneName,
                   'fastPolicyName': fastPolicyName})

        storageConfigService = (
            self.utils.find_storage_configuration_service(
                self.conn, storageSystemName))

        defaultStorageGroupInstanceName = (
            self._get_or_create_default_storage_group(
                self.conn, storageSystemName, cloneDict, cloneName,
                fastPolicyName))
        if defaultStorageGroupInstanceName is None:
            exceptionMessage = (_(
                "Unable to create or get default storage group for FAST "
                "policy: %(fastPolicyName)s. ")
                % {'fastPolicyName': fastPolicyName})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(
                data=exceptionMessage)

        self._add_volume_to_default_storage_group_on_create(
            cloneDict, cloneName, storageConfigService, storageSystemName,
            fastPolicyName)

    def _delete_volume(self, volume):
        """Helper function to delete the specified volume.

        :param volume: volume object to be deleted
        :returns: cloneDict the cloned volume dictionary
        """

        volumeName = volume['name']
        rc = -1
        errorRet = (rc, volumeName)

        extraSpecs = self._initial_setup(volume)
        self.conn = self._get_ecom_connection()

        volumeInstance = self._find_lun(volume)
        if volumeInstance is None:
            LOG.error(_LE(
                "Volume %(name)s not found on the array. "
                "No volume to delete."),
                {'name': volumeName})
            return errorRet

        storageConfigService = self.utils.find_storage_configuration_service(
            self.conn, volumeInstance['SystemName'])

        deviceId = volumeInstance['DeviceID']

        if extraSpecs[ISV3]:
            storageGroupName = self.utils.get_v3_storage_group_name(
                extraSpecs[POOL], extraSpecs[SLO], extraSpecs[WORKLOAD])
            rc = self._delete_from_pool_v3(
                storageConfigService, volumeInstance, volumeName,
                deviceId, storageGroupName)
        else:
            rc = self._delete_from_pool(storageConfigService, volumeInstance,
                                        volumeName, deviceId,
                                        extraSpecs[FASTPOLICY])
        return (rc, volumeName)

    def _pre_check_for_deletion(self, controllerConfigurationService,
                                volumeInstanceName, volumeName):
        """Check is volume is part of a storage group prior to delete

        Log a warning if volume is part of storage group

        :param controllerConfigurationService: controller configuration service
        :param volumeInstanceName: volume instance name
        :param volumeName: volume name (string)
        :return storageGroupInstanceNames
        """
        storageGroupInstanceNames = (
            self.masking.get_associated_masking_groups_from_device(
                self.conn, volumeInstanceName))
        if storageGroupInstanceNames is not None and \
                len(storageGroupInstanceNames) >= 1:
            LOG.warn(_LW(
                "Pre check for deletion "
                "Volume: %(volumeName)s is part of a storage group "
                "Attempting removal from %(storageGroupInstanceNames)s"),
                {'volumeName': volumeName,
                 'storageGroupInstanceNames': storageGroupInstanceNames})
            for storageGroupInstanceName in storageGroupInstanceNames:
                self.provision.remove_device_from_storage_group(
                    self.conn, controllerConfigurationService,
                    storageGroupInstanceName,
                    volumeInstanceName, volumeName)
        return storageGroupInstanceNames

    def _find_lunmasking_scsi_protocol_controller(self, storageSystemName,
                                                  connector):
        """Find LunMaskingSCSIProtocolController for the local host.

        Find out how many volumes are mapped to a host
        associated to the LunMaskingSCSIProtocolController.

        :param storageSystemName: the storage system name
        :param connector: volume object to be deleted
        :returns: foundControllerInstanceName
        """

        foundControllerInstanceName = None
        initiators = self._find_initiator_names(connector)

        storageSystemInstanceName = self.utils.find_storageSystem(
            self.conn, storageSystemName)
        controllerInstanceNames = self.conn.AssociatorNames(
            storageSystemInstanceName,
            ResultClass='EMC_LunMaskingSCSIProtocolController')

        for controllerInstanceName in controllerInstanceNames:
            try:
                # This is a check to see if the controller has
                # been deleted.
                self.conn.GetInstance(controllerInstanceName)
                storageHardwareIdInstances = self.conn.Associators(
                    controllerInstanceName,
                    ResultClass='EMC_StorageHardwareID')
                for storageHardwareIdInstance in storageHardwareIdInstances:
                    # If EMC_StorageHardwareID matches the initiator, we
                    # found the existing EMC_LunMaskingSCSIProtocolController.
                    hardwareid = storageHardwareIdInstance['StorageID']
                    for initiator in initiators:
                        if hardwareid.lower() == initiator.lower():
                            # This is a check to see if the controller
                            # has been deleted.
                            instance = self.utils.get_existing_instance(
                                self.conn, controllerInstanceName)
                            if instance is None:
                                # Skip this controller as it doesn't exist
                                # any more
                                pass
                            break

                if foundControllerInstanceName is not None:
                    break
            except pywbem.cim_operations.CIMError as arg:
                instance = self.utils.process_exception_args(
                    arg, controllerInstanceName)
                if instance is None:
                    # Skip this controller as it doesn't exist any more.
                    pass

            if foundControllerInstanceName is not None:
                break

        LOG.debug("LunMaskingSCSIProtocolController for storage system "
                  "%(storage_system)s and initiator %(initiator)s is "
                  "%(ctrl)s.",
                  {'storage_system': storageSystemName,
                   'initiator': initiators,
                   'ctrl': foundControllerInstanceName})
        return foundControllerInstanceName

    def get_num_volumes_mapped(self, volume, connector):
        """Returns how many volumes are in the same zone as the connector.

        Find out how many volumes are mapped to a host
        associated to the LunMaskingSCSIProtocolController

        :param volume: volume object to be deleted
        :param connector: volume object to be deleted
        :returns: int numVolumesMapped
        """

        volumename = volume['name']
        vol_instance = self._find_lun(volume)
        if vol_instance is None:
            msg = (_("Volume %(name)s not found on the array. "
                     "Cannot determine if there are volumes mapped.")
                   % {'name': volumename})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        storage_system = vol_instance['SystemName']

        ctrl = self._find_lunmasking_scsi_protocol_controller(
            storage_system,
            connector)

        LOG.debug("LunMaskingSCSIProtocolController for storage system "
                  "%(storage)s and %(connector)s is %(ctrl)s.",
                  {'storage': storage_system,
                   'connector': connector,
                   'ctrl': ctrl})

        # return 0 if masking view does not exist
        if ctrl is None:
            return 0

        associators = self.conn.Associators(
            ctrl,
            ResultClass='EMC_StorageVolume')

        numVolumesMapped = len(associators)

        LOG.debug("Found %(numVolumesMapped)d volumes on storage system "
                  "%(storage)s mapped to %(connector)s.",
                  {'numVolumesMapped': numVolumesMapped,
                   'storage': storage_system,
                   'connector': connector})

        return numVolumesMapped

    def _delete_snapshot(self, snapshot):
        """Helper function to delete the specified snapshot.

        :param snapshot: snapshot object to be deleted
        :returns: None
        """
        LOG.debug("Entering delete_snapshot.")

        snapshotname = snapshot['name']
        LOG.info(_LI("Delete Snapshot: %(snapshot)s "),
                 {'snapshot': snapshotname})

        extraSpecs = self._initial_setup(snapshot)
        self.conn = self._get_ecom_connection()

        if not extraSpecs[ISV3]:
            snapshotInstance = self._find_lun(snapshot)
            storageSystem = snapshotInstance['SystemName']

            # wait for it to fully sync in case there is an ongoing
            # create volume from snapshot request
            syncName = self.utils.find_sync_sv_by_target(
                self.conn, storageSystem, snapshotInstance, True)

            if syncName is None:
                LOG.info(_LI(
                    "Snapshot: %(snapshot)s: not found on the array. "),
                    {'snapshot': snapshotname})
            else:
                repservice = self.utils.find_replication_service(self.conn,
                                                                 storageSystem)
                if repservice is None:
                    exception_message = (_LE(
                        "Cannot find Replication Service to"
                        " delete snapshot %s."),
                        snapshotname)
                    raise exception.VolumeBackendAPIException(
                        data=exception_message)
                # break the replication relationship
                LOG.debug("Deleting snap relationship: Target: %(snapshot)s "
                          " Method: ModifyReplicaSynchronization "
                          "Replication Service: %(service)s  Operation: 8  "
                          "Synchronization: %(syncName)s.",
                          {'snapshot': snapshotname,
                           'service': str(repservice),
                           'syncName': str(syncName)})

                self.provision.delete_clone_relationship(
                    self.conn, repservice, syncName, True)

        # delete the target device
        self._delete_volume(snapshot)

    def create_consistencygroup(self, context, group):
        """Creates a consistencygroup

        :param context:
        :param group: the group object to be created
        :returns:
        """
        LOG.info(_LI("Create Consistency Group: %(group)s"),
                 {'group': group['id']})

        modelUpdate = {'status': 'available'}

        # Temp only - asking Xing why it is postfixed by comma
        volumeTypeId = group['volume_type_id'].replace(",", "")

        cgName = self.utils.truncate_string(group['id'], 8)

        extraSpecs = self._initial_setup(None, volumeTypeId)

        poolInstanceName, storageSystem = (  # @UnusedVariable
            self._get_pool_and_storage_system(extraSpecs))

        self.conn = self._get_ecom_connection()

        # find storage system
        try:
            replicationService = self.utils.find_replication_service(
                self.conn, storageSystem)
            self.provision.create_consistency_group(
                self.conn, replicationService, cgName)
        except Exception as ex:
            LOG.error(_LE("Exception: %(ex)s"), {'ex': ex})
            exceptionMessage = (_("Failed to create consistency group:"
                                  " %(cgName)s. ")
                                % {'cgName': cgName})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        return modelUpdate

    def delete_consistencygroup(self, context, group, volumes):
        """Deletes a consistency group.

        :param context:
        :param group: the group object to be deleted
        :returns:
        """
        LOG.info(_LI("Delete Consistency Group: %(group)s"),
                 {'group': group['id']})

        cgName = self.utils.truncate_string(group['id'], 8)

        modelUpdate = {}
        modelUpdate['status'] = group['status']

        # Temp only - asking Xing why it is postfixed by comma
        volumeTypeId = group['volume_type_id'].replace(",", "")

        extraSpecs = self._initial_setup(None, volumeTypeId)

        poolInstanceName, storageSystem = (  # @UnusedVariable
            self._get_pool_and_storage_system(extraSpecs))

        try:
            replicationService = self.utils.find_replication_service(
                self.conn, storageSystem)

            storageConfigservice = (
                self.utils.find_storage_configuration_service(
                    self.conn, storageSystem))
            cgInstanceName = self._find_consistency_group(
                replicationService, cgName)
            if cgInstanceName is None:
                exception_message = (_("Cannot find CG group %s."),
                                     cgName)
                raise exception.VolumeBackendAPIException(
                    data=exception_message)

            memberInstanceNames = self._get_members_of_replication_group(
                cgInstanceName)

            self.provision.delete_consistency_group(self.conn,
                                                    replicationService,
                                                    cgInstanceName, cgName)

            # do a bulk delete, a lot faster than single deletes
            if memberInstanceNames:
                try:
                    controllerConfigurationService = (
                        self.utils.find_controller_configuration_service(
                            self.conn, storageSystem))
                    for memberInstanceName in memberInstanceNames:
                        self._pre_check_for_deletion(
                            controllerConfigurationService, memberInstanceName,
                            'Member Volume')
                    if extraSpecs[ISV3]:
                        self.provisionv3.delete_volume_from_pool(
                            self.conn, storageConfigservice,
                            memberInstanceNames, None)
                    else:
                        self.provision.delete_volume_from_pool(
                            self.conn, storageConfigservice,
                            memberInstanceNames, None)
                    for volumeRef in volumes:
                        volumeRef['status'] = 'deleted'
                except Exception:
                    for volumeRef in volumes:
                        volumeRef['status'] = 'error_deleting'
                        modelUpdate['status'] = 'error_deleting'
        except Exception as ex:
            LOG.error(_LE("Exception: %s"), ex)
            exceptionMessage = (_(
                "Failed to delete consistency group: %(cgName)s. ")
                % {'cgName': cgName})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        return modelUpdate, volumes

    def create_cgsnapshot(self, context, cgsnapshot, db):
        """Creates a cgsnapshot.

        :param context:
        :param cgsnapshot: the consistency group snapshot to be created
        :param db: cinder database
        :returns: modelUpdate, list of snapshots
        """
        consistencyGroup = db.consistencygroup_get(
            context, cgsnapshot['consistencygroup_id'])

        LOG.info(_LI(
            "Create snapshot for Consistency Group %(cgId)s "
            "cgsnapshotID: %(cgsnapshot)s "),
            {'cgsnapshot': cgsnapshot['id'],
             'cgId': cgsnapshot['consistencygroup_id']})

        cgName = self.utils.truncate_string(
            cgsnapshot['consistencygroup_id'], 8)

        modelUpdate = {'status': 'available'}

        volumeTypeId = consistencyGroup['volume_type_id'].replace(",", "")
        extraSpecs = self._initial_setup(None, volumeTypeId)
        self.conn = self._get_ecom_connection()

        poolInstanceName, storageSystem = (  # @UnusedVariable
            self._get_pool_and_storage_system(extraSpecs))

        try:
            replicationService = self.utils.find_replication_service(
                self.conn, storageSystem)

            cgInstanceName = (
                self._find_consistency_group(replicationService, cgName))
            if cgInstanceName is None:
                exception_message = (_("Cannot find CG group %s."), cgName)
                raise exception.VolumeBackendAPIException(
                    data=exception_message)

            memberInstanceNames = self._get_members_of_replication_group(
                cgInstanceName)

            # create the target consistency group
            targetCgName = self.utils.truncate_string(cgsnapshot['id'], 8)
            self.provision.create_consistency_group(
                self.conn, replicationService, targetCgName)
            targetCgInstanceName = self._find_consistency_group(
                replicationService, targetCgName)
            LOG.info(_LI("Create target consistency group %(targetCg)s "),
                     {'targetCg': targetCgInstanceName})

            for memberInstanceName in memberInstanceNames:
                volInstance = self.conn.GetInstance(
                    memberInstanceName, LocalOnly=False)
                numOfBlocks = volInstance['NumberOfBlocks']
                blockSize = volInstance['BlockSize']
                volumeSizeInbits = numOfBlocks * blockSize

                targetVolumeName = 'targetVol'
                volume = {'size': int(self.utils.convert_bits_to_gbs(
                    volumeSizeInbits))}

                if extraSpecs[ISV3]:
                    rc, volumeDict, storageSystemName = (  # @UnusedVariable
                        self._create_v3_volume(
                            volume, extraSpecs,
                            targetVolumeName, volumeSizeInbits))
                else:
                    rc, volumeDict, storageSystemName = (  # @UnusedVariable
                        self._create_composite_volume(
                            volume, extraSpecs,
                            targetVolumeName, volumeSizeInbits))
                targetVolumeInstance = self.utils.find_volume_instance(
                    self.conn, volumeDict, targetVolumeName)
                LOG.debug("Create target volume for member volume "
                          "source volume: %(memberVol)s"
                          "target volume %(targetVol)s",
                          {'memberVol': memberInstanceName,
                           'targetVol': targetVolumeInstance.path})
                self.provision.add_volume_to_cg(self.conn,
                                                replicationService,
                                                targetCgInstanceName,
                                                targetVolumeInstance.path,
                                                targetCgName,
                                                targetVolumeName)

            # less than 5 characters relationship name
            relationName = self.utils.truncate_string(cgsnapshot['id'], 5)
            if extraSpecs[ISV3]:
                rc, job = (  # @UnusedVariable
                    self.provisionv3.create_group_replica(
                        self.conn, replicationService, cgInstanceName,
                        targetCgInstanceName, relationName))
            else:
                rc, job = (  # @UnusedVariable
                    self.provision.create_group_replica(
                        self.conn, replicationService, cgInstanceName,
                        targetCgInstanceName, relationName))
            # break the replica group relationship
            rgSyncInstanceName = self.utils.find_group_sync_rg_by_target(
                self.conn, storageSystem, targetCgInstanceName, True)
            if rgSyncInstanceName is not None:
                repservice = self.utils.find_replication_service(
                    self.conn, storageSystem)
                if repservice is None:
                    exception_message = (_(
                        "Cannot find Replication service on system %s"),
                        storageSystem)
                    raise exception.VolumeBackendAPIException(
                        data=exception_message)
            if extraSpecs[ISV3]:
                # operation 7: dissolve for snapVx
                operation = self.utils.get_num(9, '16')
                self.provisionv3.break_replication_relationship(
                    self.conn, repservice, rgSyncInstanceName, operation)
            else:
                self.provision.delete_clone_relationship(self.conn, repservice,
                                                         rgSyncInstanceName)

        except Exception as ex:
            modelUpdate['status'] = 'error'
            self.utils.populate_cgsnapshot_status(
                context, db, cgsnapshot['id'], modelUpdate['status'])
            LOG.error(_LE("Exception: %(ex)s"), {'ex': ex})
            exceptionMessage = (_("Failed to create snapshot for cg:"
                                  " %(cgName)s. ")
                                % {'cgName': cgName})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        snapshots = self.utils.populate_cgsnapshot_status(
            context, db, cgsnapshot['id'], modelUpdate['status'])
        return modelUpdate, snapshots

    def delete_cgsnapshot(self, context, cgsnapshot, db):
        """delete a cgsnapshot.

        :param context:
        :param cgsnapshot: the consistency group snapshot to be created
        :param db: cinder database
        :returns: modelUpdate, list of snapshots
        """
        consistencyGroup = db.consistencygroup_get(
            context, cgsnapshot['consistencygroup_id'])
        snapshots = db.snapshot_get_all_for_cgsnapshot(
            context, cgsnapshot['id'])

        LOG.info(_LI(
            "Delete snapshot for source CG %(cgId)s "
            "cgsnapshotID: %(cgsnapshot)s"),
            {'cgsnapshot': cgsnapshot['id'],
             'cgId': cgsnapshot['consistencygroup_id']})

        modelUpdate = {'status': 'deleted'}
        volumeTypeId = consistencyGroup['volume_type_id'].replace(",", "")
        extraSpecs = self._initial_setup(None, volumeTypeId)
        self.conn = self._get_ecom_connection()

        poolInstanceName, storageSystem = (  # @UnusedVariable
            self._get_pool_and_storage_system(extraSpecs))

        try:
            targetCgName = self.utils.truncate_string(cgsnapshot['id'], 8)
            modelUpdate, snapshots = self._delete_cg_and_members(
                storageSystem, extraSpecs, targetCgName, modelUpdate,
                snapshots)
        except Exception as ex:
            modelUpdate['status'] = 'error_deleting'
            self.utils.populate_cgsnapshot_status(
                context, db, cgsnapshot['id'], modelUpdate['status'])
            LOG.error(_LE("Exception: %(ex)s"), {'ex': ex})
            exceptionMessage = (_("Failed to delete snapshot for cg: "
                                  "%(cgId)s. ")
                                % {'cgId': cgsnapshot['consistencygroup_id']})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        snapshots = self.utils.populate_cgsnapshot_status(
            context, db, cgsnapshot['id'], modelUpdate['status'])
        return modelUpdate, snapshots

    def _find_consistency_group(self, replicationService, cgName):
        """Finds a CG given its name.

        :param replicationService: the replication service
        :param cgName: the consistency group name
        :returns:
        """
        foundCgInstanceName = None
        cgInstanceNames = (
            self.conn.AssociatorNames(replicationService,
                                      ResultClass='CIM_ReplicationGroup'))

        for cgInstanceName in cgInstanceNames:
            instance = self.conn.GetInstance(cgInstanceName, LocalOnly=False)
            if cgName == instance['ElementName']:
                foundCgInstanceName = cgInstanceName
                break

        return foundCgInstanceName

    def _get_members_of_replication_group(self, cgInstanceName):
        """Get the members of consistency group.

        :param cgInstanceName: the CG instance name
        :returns:
        """
        memberInstanceNames = self.conn.AssociatorNames(
            cgInstanceName,
            AssocClass='CIM_OrderedMemberOfCollection')

        return memberInstanceNames

    def _create_composite_volume(
            self, volume, extraSpecs, volumeName, volumeSize):
        """create a composite volume (V2)

        :param volume: the volume object
        :param extraSpecs:
        :param volumeName:
        :param volumeSize:
        :returns:
        """
        memberCount, errorDesc = self.utils.determine_member_count(
            volume['size'], extraSpecs[MEMBERCOUNT], extraSpecs[COMPOSITETYPE])
        if errorDesc is not None:
            exceptionMessage = (_("The striped meta count of %(memberCount)s "
                                  "is too small for volume: %(volumeName)s. "
                                  "with size %(volumeSize)s ")
                                % {'memberCount': memberCount,
                                   'volumeName': volumeName,
                                   'volumeSize': volume['size']})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        poolInstanceName, storageSystemName = (
            self._get_pool_and_storage_system(extraSpecs))

        LOG.debug("Create Volume: %(volume)s  Pool: %(pool)s  "
                  "Storage System: %(storageSystem)s "
                  "Size: %(size)lu ",
                  {'volume': volumeName,
                   'pool': poolInstanceName,
                   'storageSystem': storageSystemName,
                   'size': volumeSize})

        elementCompositionService = (
            self.utils.find_element_composition_service(self.conn,
                                                        storageSystemName))

        storageConfigService = self.utils.find_storage_configuration_service(
            self.conn, storageSystemName)

        # If FAST is intended to be used we must first check that the pool
        # is associated with the correct storage tier
        if extraSpecs[FASTPOLICY] is not None:
            foundPoolInstanceName = self.fast.get_pool_associated_to_policy(
                self.conn, extraSpecs[FASTPOLICY], extraSpecs[ARRAY],
                storageConfigService, poolInstanceName)
            if foundPoolInstanceName is None:
                exceptionMessage = (_("Pool: %(poolName)s. "
                                      "is not associated to storage tier for "
                                      "fast policy %(fastPolicy)s.")
                                    % {'poolName': extraSpecs[POOL],
                                       'fastPolicy': extraSpecs[FASTPOLICY]})
                LOG.error(exceptionMessage)
                raise exception.VolumeBackendAPIException(
                    data=exceptionMessage)

        compositeType = self.utils.get_composite_type(
            extraSpecs[COMPOSITETYPE])

        volumeDict, rc = self.provision.create_composite_volume(
            self.conn, elementCompositionService, volumeSize, volumeName,
            poolInstanceName, compositeType, memberCount)

        # Now that we have already checked that the pool is associated with
        # the correct storage tier and the volume was successfully created
        # add the volume to the default storage group created for
        # volumes in pools associated with this fast policy
        if extraSpecs[FASTPOLICY]:
            LOG.info(_LI(
                "Adding volume: %(volumeName)s to default storage group"
                " for FAST policy: %(fastPolicyName)s "),
                {'volumeName': volumeName,
                 'fastPolicyName': extraSpecs[FASTPOLICY]})
            defaultStorageGroupInstanceName = (
                self._get_or_create_default_storage_group(
                    self.conn, storageSystemName, volumeDict,
                    volumeName, extraSpecs[FASTPOLICY]))
            if not defaultStorageGroupInstanceName:
                exceptionMessage = (_(
                    "Unable to create or get default storage group for "
                    "FAST policy: %(fastPolicyName)s. ")
                    % {'fastPolicyName': extraSpecs[FASTPOLICY]})
                LOG.error(exceptionMessage)
                raise exception.VolumeBackendAPIException(
                    data=exceptionMessage)

            self._add_volume_to_default_storage_group_on_create(
                volumeDict, volumeName, storageConfigService,
                storageSystemName, extraSpecs[FASTPOLICY])
        return rc, volumeDict, storageSystemName

    def _create_v3_volume(
            self, volume, extraSpecs, volumeName, volumeSize):
        """create a volume (V3)

        :param volume: the volume object
        :param extraSpecs:
        :param volumeName:
        :param volumeSize:
        :returns:
        """
        isValidSLO, isValidWorkload = self.utils.verify_slo_workload(
            extraSpecs[SLO], extraSpecs[WORKLOAD])

        if not isValidSLO or not isValidWorkload:
            exceptionMessage = (_(
                "Either SLO: %(slo)s or workload %(workload)s is invalid. "
                "Examine previous error statement for valid values")
                % {'slo': extraSpecs[SLO],
                   'workload': extraSpecs[WORKLOAD]})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        poolInstanceName, storageSystemName = (
            self._get_pool_and_storage_system(extraSpecs))

        LOG.debug("Create Volume: %(volume)s  Pool: %(pool)s  "
                  "Storage System: %(storageSystem)s "
                  "Size: %(size)lu ",
                  {'volume': volumeName,
                   'pool': poolInstanceName,
                   'storageSystem': storageSystemName,
                   'size': volumeSize})

        storageConfigService = self.utils.find_storage_configuration_service(
            self.conn, storageSystemName)

        # check the SLO range
        maximumVolumeSize, minimumVolumeSize = (
            self.provisionv3.get_volume_range(
                self.conn, storageConfigService, poolInstanceName,
                extraSpecs[SLO], extraSpecs[WORKLOAD]))
        if not self.utils.is_in_range(
                volumeSize, maximumVolumeSize, minimumVolumeSize):
            LOG.warn(_LW(
                "Volume: %(volume)s with size: %(volumeSize)s bits "
                "is not in the Performance Capacity range : "
                "%(minimumVolumeSize)s-%(maximumVolumeSize)s bits. "
                "for SLO:%(slo)s and workload:%(workload)s. "
                "Unpredictable results may occur. "),
                {'volume': volumeName,
                 'volumeSize': volumeSize,
                 'minimumVolumeSize': minimumVolumeSize,
                 'maximumVolumeSize': maximumVolumeSize,
                 'slo': extraSpecs[SLO],
                 'workload': extraSpecs[WORKLOAD]
                 })

        # A volume created without specifying a storage group during
        # creation time is allocated from the default SRP pool and
        # assigned the optimized SLO
        sgInstanceName = self._get_or_create_storage_group_v3(
            extraSpecs[POOL], extraSpecs[SLO], extraSpecs[WORKLOAD],
            storageSystemName)
        volumeDict, rc = self.provisionv3.create_volume_from_sg(
            self.conn, storageConfigService, volumeName,
            sgInstanceName, volumeSize)

        return rc, volumeDict, storageSystemName

    def _get_or_create_storage_group_v3(
            self, poolName, slo, workload, storageSystemName):
        """Get or create storage group_v3 (V3)

        :param poolName: the SRP pool nsmr
        :param slo: the SLO
        :param workload: the workload
        :param storageSystemName: storage system name
        :returns:
        """
        storageGroupName = self.utils.get_v3_storage_group_name(
            poolName, slo, workload)
        controllerConfigService = (
            self.utils.find_controller_configuration_service(
                self.conn, storageSystemName))
        sgInstanceName = self.utils.find_storage_masking_group(
            self.conn, controllerConfigService, storageGroupName)
        if sgInstanceName is None:
            sgInstanceName = self.provisionv3.create_storage_group_v3(
                self.conn, controllerConfigService, storageGroupName,
                poolName, slo, workload)

        return sgInstanceName

    def _extend_composite_volume(self, volumeInstance, volumeName,
                                 newSize, additionalVolumeSize):
        """extend a composite volume (V2)

        :param volumeInstance: the volume instance
        :param volumeName: the name of the volume
        :param newSize: in GBs
        :param additionalVolumeSize:
        :returns:
        """
        # Is the volume extendable.
        isConcatenated = self.utils.check_if_volume_is_extendable(
            self.conn, volumeInstance)
        if 'True' not in isConcatenated:
            exceptionMessage = (_(
                "Volume: %(volumeName)s is not a concatenated volume. "
                "You can only perform extend on concatenated volume. "
                "Exiting...")
                % {'volumeName': volumeName})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)
        else:
            compositeType = self.utils.get_composite_type(CONCATENATED)

        LOG.debug("Extend Volume: %(volume)s  New size: %(newSize)s GBs",
                  {'volume': volumeName,
                   'newSize': newSize})

        deviceId = volumeInstance['DeviceID']
        storageSystemName = volumeInstance['SystemName']
        LOG.debug(
            "Device ID: %(deviceid)s: Storage System: "
            "%(storagesystem)s",
            {'deviceid': deviceId,
             'storagesystem': storageSystemName})

        storageConfigService = self.utils.find_storage_configuration_service(
            self.conn, storageSystemName)

        elementCompositionService = (
            self.utils.find_element_composition_service(
                self.conn, storageSystemName))

        # create a volume to the size of the
        # newSize - oldSize = additionalVolumeSize
        unboundVolumeInstance = self._create_and_get_unbound_volume(
            self.conn, storageConfigService, volumeInstance.path,
            additionalVolumeSize)
        if unboundVolumeInstance is None:
            exceptionMessage = (_(
                "Error Creating unbound volume on an Extend operation"))
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        # add the new unbound volume to the original composite volume
        rc, modifiedVolumeDict = (
            self._modify_and_get_composite_volume_instance(
                self.conn, elementCompositionService, volumeInstance,
                unboundVolumeInstance.path, volumeName, compositeType))
        if modifiedVolumeDict is None:
            exceptionMessage = (_(
                "On an Extend Operation, error adding volume to composite "
                "volume: %(volumename)s. ")
                % {'volumename': volumeName})
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(data=exceptionMessage)

        return rc, modifiedVolumeDict

    def _slo_workload_migration(self, volumeInstance, volume, host,
                                volumeName, volumeStatus,
                                extraSpecs, newType):
        """migrate from SLO/Workload combination to another (V3)

        :param volumeInstance: the volume instance
        :param volume: the volume object
        :param host: the host object
        :param volumeName: the name of the volume
        :param volumeStatus: the volume status
        :param extraSpecs: the extra specs dict
        :param newType:
        :returns: boolean
        """
        volumeInstanceName = volumeInstance.path
        isValid, targetSlo, targetWorkload = (
            self._is_valid_for_storage_assisted_migration_v3(
                volumeInstanceName, host, extraSpecs[ARRAY],
                extraSpecs[POOL], volumeName, volumeStatus))

        storageSystemName = volumeInstance['SystemName']

        if not isValid:
            LOG.error(_LE(
                "Volume %(name)s is not suitable for storage "
                "assisted migration using retype"),
                {'name': volumeName})
            return False
        if volume['host'] != host['host']:
            LOG.debug(
                "Retype Volume %(name)s from source host %(sourceHost)s "
                "to target host %(targetHost)s",
                {'name': volumeName,
                 'sourceHost': volume['host'],
                 'targetHost': host['host']})
            return self._migrate_volume_v3(
                volume, volumeInstance, extraSpecs[POOL], targetSlo,
                targetWorkload, storageSystemName, newType)

        return False

    def _migrate_volume_v3(
            self, volume, volumeInstance, poolName, targetSlo,
            targetWorkload, storageSystemName, newType):
        """migrate from one slo/workload combination to another (V3)

        This requires moving the volume from it's current SG to a
        new or existing SG that has the target attributes

        :param volume: the volume object
        :param volumeInstance: the volume instance
        :param poolName: the SRP Pool Name
        :param targetSlo: the target SLO
        :param targetWorkload: the target workload
        :param storageSystemName: the storage system name
        :param newType:
        :returns: boolean
        """
        volumeName = volume['name']

        controllerConfigService = (
            self.utils.find_controller_configuration_service(
                self.conn, storageSystemName))

        foundStorageGroupInstanceName = (
            self.utils.get_storage_group_from_volume(
                self.conn, volumeInstance.path))
        if foundStorageGroupInstanceName is None:
            LOG.warn(_LW(
                "Volume : %(volumeName)s is not currently "
                "belonging to any storage group "),
                {'volumeName': volumeName})
        else:
            self.provision.remove_device_from_storage_group(
                self.conn,
                controllerConfigService,
                foundStorageGroupInstanceName,
                volumeInstance.path,
                volumeName)
            # check that it has been removed
            sgFromVolRemovedInstanceName = (
                self.utils.wrap_get_storage_group_from_volume(
                    self.conn, volumeInstance.path))
            if sgFromVolRemovedInstanceName is not None:
                LOG.error(_LE(
                    "Volume : %(volumeName)s has not been "
                    "removed from source storage group %(storageGroup)s"),
                    {'volumeName': volumeName,
                     'storageGroup': sgFromVolRemovedInstanceName})
                return False

        storageGroupName = self.utils.get_v3_storage_group_name(
            poolName, targetSlo, targetWorkload)

        targetSgInstanceName = self._get_or_create_storage_group_v3(
            poolName, targetSlo, targetWorkload, storageSystemName)
        if targetSgInstanceName is None:
            LOG.error(_LE(
                "Failed to get or create storage group %(storageGroupName)s "),
                {'storageGroupName': storageGroupName})
            return False

        self.masking.add_volume_to_storage_group(
            self.conn, controllerConfigService, targetSgInstanceName,
            volumeInstance, volumeName, storageGroupName)
        # check that it has been added
        sgFromVolAddedInstanceName = (
            self.utils.get_storage_group_from_volume(
                self.conn, volumeInstance.path))
        if sgFromVolAddedInstanceName is None:
            LOG.error(_LE(
                "Volume : %(volumeName)s has not been "
                "added to target storage group %(storageGroup)s"),
                {'volumeName': volumeName,
                 'storageGroup': targetSgInstanceName})
            return False

        return True

    def _pool_migration(self, volumeInstance, volume, host,
                        volumeName, volumeStatus,
                        fastPolicyName, newType):
        """migrate from one pool to another (V2)

        :param volumeInstance: the volume instance
        :param volume: the volume object
        :param host: the host object
        :param volumeName: the name of the volume
        :param volumeStatus: the volume status
        :param fastPolicyName: the FAST policy Name
        :param newType:
        :returns: boolean
        """
        storageSystemName = volumeInstance['SystemName']
        isValid, targetPoolName, targetFastPolicyName = (
            self._is_valid_for_storage_assisted_migration(
                volumeInstance.path, host, storageSystemName,
                volumeName, volumeStatus))

        if not isValid:
            LOG.error(_LE(
                "Volume %(name)s is not suitable for storage "
                "assisted migration using retype"),
                {'name': volumeName})
            return False
        if volume['host'] != host['host']:
            LOG.debug(
                "Retype Volume %(name)s from source host %(sourceHost)s "
                "to target host %(targetHost)s",
                {'name': volumeName,
                 'sourceHost': volume['host'],
                 'targetHost': host['host']})
            return self._migrate_volume(
                volume, volumeInstance, targetPoolName, targetFastPolicyName,
                fastPolicyName, newType)

        return False

    def _update_pool_stats(
            self, emcConfigFileName, backendName, arrayName, poolName):
        """update pool statistics (V2)

        :param emcConfigFileName: the EMC configuration file
        :param backendName: the backend name
        :param arrayName: the array Name
        :returns: location_info, total_capacity_gb, free_capacity_gb
        """
        # This value can be None
        fastPolicyName = self.utils.parse_fast_policy_name_from_file(
            emcConfigFileName)
        if fastPolicyName is not None:
            LOG.debug(
                "Fast policy %(fastPolicyName)s is enabled on %(arrayName)s. ",
                {'fastPolicyName': fastPolicyName,
                 'arrayName': arrayName})
        else:
            LOG.debug(
                "No Fast policy for Array:%(arrayName)s "
                "backend:%(backendName)s",
                {'arrayName': arrayName,
                 'backendName': backendName})

        storageSystemInstanceName = self.utils.find_storageSystem(
            self.conn, arrayName)
        isTieringPolicySupported = (
            self.fast.is_tiering_policy_enabled_on_storage_system(
                self.conn, storageSystemInstanceName))

        if (fastPolicyName is not None and
                isTieringPolicySupported is True):  # FAST enabled
            total_capacity_gb, free_capacity_gb = (
                self.fast.get_capacities_associated_to_policy(
                    self.conn, arrayName, fastPolicyName))
            LOG.info(
                "FAST: capacity stats for policy %(fastPolicyName)s on "
                "array %(arrayName)s (total_capacity_gb=%(total_capacity_gb)lu"
                ", free_capacity_gb=%(free_capacity_gb)lu"
                % {'fastPolicyName': fastPolicyName,
                   'arrayName': arrayName,
                   'total_capacity_gb': total_capacity_gb,
                   'free_capacity_gb': free_capacity_gb})
        else:  # NON-FAST
            total_capacity_gb, free_capacity_gb = (
                self.utils.get_pool_capacities(self.conn, poolName, arrayName))
            LOG.info(
                "NON-FAST: capacity stats for pool %(poolName)s on array "
                "%(arrayName)s (total_capacity_gb=%(total_capacity_gb)lu, "
                "free_capacity_gb=%(free_capacity_gb)lu"
                % {'poolName': poolName,
                   'arrayName': arrayName,
                   'total_capacity_gb': total_capacity_gb,
                   'free_capacity_gb': free_capacity_gb})

        if poolName is None:
            LOG.debug("Unable to get the poolName for location_info")
        if arrayName is None:
            LOG.debug("Unable to get the arrayName for location_info")
        if fastPolicyName is None:
            LOG.debug("FAST is not enabled for this configuration: "
                      "%(emcConfigFileName)s",
                      {'emcConfigFileName': emcConfigFileName})

        location_info = ("%(arrayName)s#%(poolName)s#%(policyName)s"
                         % {'arrayName': arrayName,
                            'poolName': poolName,
                            'policyName': fastPolicyName})

        return location_info, total_capacity_gb, free_capacity_gb

    def _set_v2_extra_specs(self, extraSpecs, configurationFile, arrayName):
        """set the VMAX V2 extra specs

        :param extraSpecs: the extraSpecs (input)
        :param configurationFile: the EMC configuration file
        :param arrayName: the array serial number
        :returns: extraSpecs (out)
        """
        try:
            stripedMetaCount = extraSpecs[STRIPECOUNT]
            extraSpecs[MEMBERCOUNT] = stripedMetaCount
            extraSpecs[COMPOSITETYPE] = STRIPED

            LOG.debug(
                "There are: %(stripedMetaCount)s striped metas in "
                "the extra specs",
                {'stripedMetaCount': stripedMetaCount})
        except Exception:
            memberCount = '1'
            extraSpecs[MEMBERCOUNT] = memberCount
            extraSpecs[COMPOSITETYPE] = CONCATENATED
            LOG.debug("StripedMetaCount is not in the extra specs")
            pass

        poolName = self.utils.parse_pool_name_from_file(configurationFile)
        if poolName is None:
            exceptionMessage = (_(
                "The pool cannot be null. The pool must be configured "
                "either in the extra specs or in the EMC configuration "
                "file corresponding to the Volume Type. "))
            LOG.error(exceptionMessage)
            raise exception.VolumeBackendAPIException(
                data=exceptionMessage)

        # Get the FAST policy from the file this value can be None if the
        # user doesnt want to associate with any FAST policy
        fastPolicyName = self.utils.parse_fast_policy_name_from_file(
            configurationFile)
        if fastPolicyName is not None:
            LOG.debug("The fast policy name is: %(fastPolicyName)s. ",
                      {'fastPolicyName': fastPolicyName})

        extraSpecs[POOL] = poolName
        extraSpecs[ARRAY] = arrayName
        extraSpecs[FASTPOLICY] = fastPolicyName
        extraSpecs[ISV3] = False

        LOG.debug("Pool is: %(pool)s "
                  "Array is: %(array)s "
                  "FastPolicy is: %(fastPolicy)s "
                  "CompositeType is: %(compositeType)s "
                  "MemberCount is: %(memberCount)s ",
                  {'pool': extraSpecs[POOL],
                   'array': extraSpecs[ARRAY],
                   'fastPolicy': extraSpecs[FASTPOLICY],
                   'compositeType': extraSpecs[COMPOSITETYPE],
                   'memberCount': extraSpecs[MEMBERCOUNT]})
        return extraSpecs

    def _set_v3_extra_specs(self, extraSpecs, configurationFile, arrayName):
        """set the VMAX V3 extra specs.

        If SLO or workload are not specified then the default
        values are NONE and the Optimized SLO will be assigned to the
        volume

        :param extraSpecs: the extraSpecs (input)
        :param configurationFile: the EMC configuration file
        :param arrayName: the array serial number
        :returns: extraSpecs (out)
        """
        extraSpecs[SLO] = self.utils.parse_slo_from_file(configurationFile)
        extraSpecs[WORKLOAD] = self.utils.parse_workload_from_file(
            configurationFile)
        extraSpecs[POOL] = self.utils.parse_pool_name_from_file(
            configurationFile)
        extraSpecs[ARRAY] = arrayName
        extraSpecs[ISV3] = True

        LOG.debug("Pool is: %(pool)s "
                  "Array is: %(array)s "
                  "SLO is: %(slo)s "
                  "Workload is: %(workload)s ",
                  {'pool': extraSpecs[POOL],
                   'array': extraSpecs[ARRAY],
                   'slo': extraSpecs[SLO],
                   'workload': extraSpecs[WORKLOAD]})
        return extraSpecs

    def _delete_from_pool(self, storageConfigService, volumeInstance,
                          volumeName, deviceId, fastPolicyName):
        """delete from pool (v2)

        :param storageConfigService: the storage config service
        :param volumeInstance: the volume instance
        :param volumeName: the volume Name
        :param deviceId: the device ID of the volume
        :param fastPolicyName: the FAST policy name(if it exists)
        :returns: rc
        """
        storageSystemName = volumeInstance['SystemName']
        controllerConfigurationService = (
            self.utils.find_controller_configuration_service(
                self.conn, storageSystemName))
        if fastPolicyName is not None:
            defaultStorageGroupInstanceName = (
                self.masking.remove_device_from_default_storage_group(
                    self.conn, controllerConfigurationService,
                    volumeInstance.path, volumeName, fastPolicyName))
            if defaultStorageGroupInstanceName is None:
                LOG.warn(_LW(
                    "The volume: %(volumename)s. was not first part of the "
                    "default storage group for FAST policy %(fastPolicyName)s"
                    "."),
                    {'volumename': volumeName,
                     'fastPolicyName': fastPolicyName})
                # check if it is part of another storage group
                self._pre_check_for_deletion(controllerConfigurationService,
                                             volumeInstance.path, volumeName)

        else:
            # check if volume is part of a storage group
            self._pre_check_for_deletion(controllerConfigurationService,
                                         volumeInstance.path, volumeName)

        LOG.debug("Delete Volume: %(name)s  Method: EMCReturnToStoragePool "
                  "ConfigServic: %(service)s  TheElement: %(vol_instance)s "
                  "DeviceId: %(deviceId)s ",
                  {'service': storageConfigService,
                   'name': volumeName,
                   'vol_instance': volumeInstance.path,
                   'deviceId': deviceId})
        try:
            rc = self.provision.delete_volume_from_pool(
                self.conn, storageConfigService, volumeInstance.path,
                volumeName)

        except Exception as e:
            # if we cannot successfully delete the volume then we want to
            # return the volume to the default storage group
            if (fastPolicyName is not None and
                    defaultStorageGroupInstanceName is not None and
                    storageSystemName is not None):
                assocDefaultStorageGroupName = (
                    self.fast
                    .add_volume_to_default_storage_group_for_fast_policy(
                        self.conn, controllerConfigurationService,
                        volumeInstance, volumeName, fastPolicyName))
                if assocDefaultStorageGroupName is None:
                    LOG.error(_LE(
                        "Failed to Roll back to re-add volume %(volumeName)s "
                        "to default storage group for fast policy "
                        "%(fastPolicyName)s: Please contact your sysadmin to "
                        "get the volume returned to the default "
                        "storage group"),
                        {'volumeName': volumeName,
                         'fastPolicyName': fastPolicyName})

            LOG.error(_LE("Exception: %s"), e)
            errorMessage = (_("Failed to delete volume %(volumeName)s"),
                            {'volumeName': volumeName})
            LOG.error(errorMessage)
            raise exception.VolumeBackendAPIException(data=errorMessage)

        return rc

    def _delete_from_pool_v3(self, storageConfigService, volumeInstance,
                             volumeName, deviceId, storageGroupName):
        """delete from pool (v3)

        :param storageConfigService: the storage config service
        :param volumeInstance: the volume instance
        :param volumeName: the volume Name
        :param deviceId: the device ID of the volume
        :param storageGroupName: the name of the default SG
        :returns: rc
        """
        storageSystemName = volumeInstance['SystemName']
        controllerConfigurationService = (
            self.utils.find_controller_configuration_service(
                self.conn, storageSystemName))

        # check if it is part of a storage group and delete it
        # extra logic for case when volume is the last member
        sgFromVolInstanceName = self.masking.remove_and_reset_members(
            self.conn, controllerConfigurationService, volumeInstance,
            None, volumeName, True, None, 'noReset')

        LOG.debug("Delete Volume: %(name)s  Method: EMCReturnToStoragePool "
                  "ConfigServic: %(service)s  TheElement: %(vol_instance)s "
                  "DeviceId: %(deviceId)s ",
                  {'service': storageConfigService,
                   'name': volumeName,
                   'vol_instance': volumeInstance.path,
                   'deviceId': deviceId})
        try:
            rc = self.provisionv3.delete_volume_from_pool(
                self.conn, storageConfigService, volumeInstance.path,
                volumeName)

        except Exception as e:
            # if we cannot successfully delete the volume then we want to
            # return the volume to the default storage group
            # which should be the SG it previously belonged to
            storageGroupInstanceName = self.utils.find_storage_masking_group(
                self.conn, controllerConfigurationService, storageGroupName)

            if sgFromVolInstanceName is not storageGroupInstanceName:
                LOG.debug(
                    "Volume: %(volumeName)s was not previously part of "
                    " %(storageGroupInstanceName)s. "
                    "Returning to %(storageGroupName)s",
                    {'volumeName': volumeName,
                     'storageGroupInstanceName': storageGroupInstanceName,
                     'storageGroupName': storageGroupName})

            if storageGroupInstanceName is not None:
                self.masking.add_volume_to_storage_group(
                    self.conn, controllerConfigurationService,
                    storageGroupInstanceName, volumeInstance, volumeName,
                    storageGroupName)

            LOG.error(_LE("Exception: %s"), e)
            errorMessage = (_("Failed to delete volume %(volumeName)s"),
                            {'volumeName': volumeName})
            LOG.error(errorMessage)
            raise exception.VolumeBackendAPIException(data=errorMessage)

        return rc

    def _create_clone_v2(self, repServiceInstanceName, cloneVolume,
                         sourceVolume, sourceInstance, isSnapshot,
                         extraSpecs):
        """create a clone (v2)

        :param repServiceInstanceName: the replication service
        :param cloneVolume: the clone volume object
        :param sourceVolume: the source volume object
        :param sourceInstance: the device ID of the volume
        :param isSnapshot: check to see if it is a snapshot
        :param fastPolicyName: the FAST policy name(if it exists)
        :returns: rc
        """
        # check if the source volume contains any meta devices
        metaHeadInstanceName = self.utils.get_volume_meta_head(
            self.conn, sourceInstance.path)

        if metaHeadInstanceName is None:  # simple volume
            return self._create_v2_replica_and_delete_clone_relationship(
                repServiceInstanceName, cloneVolume, sourceVolume,
                sourceInstance, None, isSnapshot, extraSpecs)
        else:  # composite volume with meta device members
            # check if the meta members' capacity
            metaMemberInstanceNames = (
                self.utils.get_meta_members_of_composite_volume(
                    self.conn, metaHeadInstanceName))
            volumeCapacities = self.utils.get_meta_members_capacity_in_bit(
                self.conn, metaMemberInstanceNames)
            LOG.debug("Volume capacities:  %(metasizes)s ",
                      {'metasizes': volumeCapacities})
            if len(set(volumeCapacities)) == 1:
                LOG.debug("Meta volume all of the same size")
                return self._create_v2_replica_and_delete_clone_relationship(
                    repServiceInstanceName, cloneVolume, sourceVolume,
                    sourceInstance, None, isSnapshot, extraSpecs)

            LOG.debug("Meta volumes are of different sizes, "
                      "%d different sizes", len(set(volumeCapacities)))

            baseTargetVolumeInstance = None
            for volumeSizeInbits in volumeCapacities:
                if baseTargetVolumeInstance is None:  # create base volume
                    baseVolumeName = "TargetBaseVol"
                    volume = {'size': int(self.utils.convert_bits_to_gbs(
                        volumeSizeInbits))}
                    r, baseVolumeDict, storageSystemName = (  # @UnusedVariable
                        self._create_composite_volume(
                            volume, extraSpecs,
                            baseVolumeName, volumeSizeInbits))
                    baseTargetVolumeInstance = self.utils.find_volume_instance(
                        self.conn, baseVolumeDict, baseVolumeName)
                    LOG.debug("Base target volume %(targetVol)s created. "
                              "capacity in bits: %(capInBits)lu ",
                              {'capInBits': volumeSizeInbits,
                               'targetVol': baseTargetVolumeInstance.path})
                else:  # create append volume
                    targetVolumeName = "MetaVol"
                    volume = {'size': int(self.utils.convert_bits_to_gbs(
                        volumeSizeInbits))}
                    storageConfigService = (
                        self.utils.find_storage_configuration_service(
                            self.conn, storageSystemName))
                    unboundVolumeInstance = (
                        self._create_and_get_unbound_volume(
                            self.conn, storageConfigService,
                            baseTargetVolumeInstance.path, volumeSizeInbits))
                    if unboundVolumeInstance is None:
                        exceptionMessage = (_(
                            "Error Creating unbound volume."))
                        LOG.error(exceptionMessage)
                        raise exception.VolumeBackendAPIException(
                            data=exceptionMessage)

                    # append the new unbound volume to the
                    # base target composite volume
                    baseTargetVolumeInstance = self.utils.find_volume_instance(
                        self.conn, baseVolumeDict, baseVolumeName)
                    elementCompositionService = (
                        self.utils.find_element_composition_service(
                            self.conn, storageSystemName))
                    compositeType = self.utils.get_composite_type(
                        extraSpecs[COMPOSITETYPE])
                    rc, modifiedVolumeDict = (  # @UnusedVariable
                        self._modify_and_get_composite_volume_instance(
                            self.conn,
                            elementCompositionService,
                            baseTargetVolumeInstance,
                            unboundVolumeInstance.path,
                            targetVolumeName,
                            compositeType))
                    if modifiedVolumeDict is None:
                        exceptionMessage = (_(
                            "Error appending volume %(volumename)s to "
                            "target base volume")
                            % {'volumename': targetVolumeName})
                        LOG.error(exceptionMessage)
                        raise exception.VolumeBackendAPIException(
                            data=exceptionMessage)

            LOG.debug("Create V2 replica for meta members of different sizes")
            return self._create_v2_replica_and_delete_clone_relationship(
                repServiceInstanceName, cloneVolume, sourceVolume,
                sourceInstance, baseTargetVolumeInstance, isSnapshot,
                extraSpecs)

    def _create_v2_replica_and_delete_clone_relationship(
            self, repServiceInstanceName, cloneVolume, sourceVolume,
            sourceInstance, targetInstance, isSnapshot, extraSpecs):
        sourceName = sourceVolume['name']
        cloneName = cloneVolume['name']
        rc, job = self.provision.create_element_replica(
            self.conn, repServiceInstanceName, cloneName, sourceName,
            sourceInstance, targetInstance)
        cloneDict = self.provision.get_volume_dict_from_job(
            self.conn, job['Job'])

        fastPolicyName = extraSpecs[FASTPOLICY]
        if isSnapshot:
            if fastPolicyName is not None:
                storageSystemName = sourceInstance['SystemName']
                self._add_clone_to_default_storage_group(
                    fastPolicyName, storageSystemName, cloneDict, cloneName)
            LOG.info("Snapshot creation %(cloneName)s completed. "
                     "Source Volume: %(sourceName)s  "
                     % {'cloneName': cloneName,
                        'sourceName': sourceName})

            return rc, cloneDict

        cloneVolume['provider_location'] = six.text_type(cloneDict)
        syncInstanceName, storageSystemName = (
            self._find_storage_sync_sv_sv(cloneVolume, sourceVolume))

        # Remove the Clone relationship so it can be used as a regular lun
        # 8 - Detach operation
        rc, job = self.provision.delete_clone_relationship(
            self.conn, repServiceInstanceName, syncInstanceName)
        if fastPolicyName is not None:
            self._add_clone_to_default_storage_group(
                fastPolicyName, storageSystemName, cloneDict, cloneName)

        return rc, cloneDict

    def get_target_wwns_from_masking_view(
            self, storageSystem, volume, connector):
        """Find target WWNs via the masking view.

        :param storageSystem: the storage system name
        :param volume: volume to be attached
        :param connector: the connector dict
        :returns: targetWwns, the target WWN list
        """
        targetWwns = []
        mvInstanceName = self.get_masking_view_by_volume(volume, connector)
        if mvInstanceName is not None:
            targetWwns = self.masking.get_target_wwns(
                self.conn, mvInstanceName)
            LOG.info("Target wwns in masking view %(maskingView)s: "
                     "%(targetWwns)s"
                     % {'maskingView': mvInstanceName,
                        'targetWwns': str(targetWwns)})
        return targetWwns

    def get_port_group_from_masking_view(self, maskingViewInstanceName):
        return self.masking.get_port_group_from_masking_view(
            self.conn, maskingViewInstanceName)

    def get_masking_view_by_volume(self, volume, connector):
        """Given volume, retrieve the masking view instance name

        :param volume: the volume
        :param connector: the connector object
        :returns maskingviewInstanceName
        """
        LOG.debug("Finding Masking View for volume %(volume)s",
                  {'volume': volume})
        volumeInstance = self._find_lun(volume)
        return self.masking.get_masking_view_by_volume(
            self.conn, volumeInstance, connector)

    def get_masking_views_by_port_group(self, portGroupInstanceName):
        """Given port group, retrieve the masking view instance name

        :param : the volume
        :param mvInstanceName: masking view instance name
        :returns: maksingViewInstanceNames
        """
        LOG.debug("Finding Masking Views for port group %(pg)s",
                  {'pg': portGroupInstanceName})
        return self.masking.get_masking_views_by_port_group(
            self.conn, portGroupInstanceName)

    def _create_replica_v3(
            self, repServiceInstanceName, cloneVolume,
            sourceVolume, sourceInstance, isSnapshot):
        """VG3R specific function, create replica for source volume,
        including clone and snapshot.

        :param repServiceInstanceName: the replication service
        :param cloneVolume: the clone volume object
        :param sourceInstance: the device ID of the volume
        :param isSnapshot: check to see if it is a snapshot
        :returns: rc
        """
        cloneName = cloneVolume['name']
        syncType = self.utils.get_num(8, '16')  # default syncType 8: clone

        # create target volume
        extraSpecs = self._initial_setup(cloneVolume)

        numOfBlocks = sourceInstance['NumberOfBlocks']
        blockSize = sourceInstance['BlockSize']
        volumeSizeInbits = numOfBlocks * blockSize

        volume = {'size':
                  int(self.utils.convert_bits_to_gbs(volumeSizeInbits))}
        rc, volumeDict, storageSystemName = (  # @UnusedVariable
            self._create_v3_volume(
                volume, extraSpecs, cloneName, volumeSizeInbits))
        targetInstance = self.utils.find_volume_instance(
            self.conn, volumeDict, cloneName)
        LOG.debug("Create replica target volume "
                  "source volume: %(sourceVol)s"
                  "target volume %(targetVol)s",
                  {'sourceVol': sourceInstance.path,
                   'targetVol': targetInstance.path})
        if isSnapshot:
            # syncType 7: snap, VG3R default snapshot is snapVx
            syncType = self.utils.get_num(7, '16')

        rc, job = (  # @UnusedVariable
            self.provisionv3.create_element_replica(
                self.conn, repServiceInstanceName, cloneName, syncType,
                sourceInstance, targetInstance))
        cloneDict = self.provisionv3.get_volume_dict_from_job(
            self.conn, job['Job'])

        cloneVolume['provider_location'] = six.text_type(cloneDict)

        syncInstanceName, storageSystemName = (  # @UnusedVariable
            self._find_storage_sync_sv_sv(cloneVolume, sourceVolume, True))

        # detach/dissolve the clone/snap relationship
        # 8 - Detach operation
        # 9 - Dissolve operation
        if isSnapshot:
            # operation 7: dissolve for snapVx
            operation = self.utils.get_num(9, '16')
        else:
            # operation 8: detach for clone
            operation = self.utils.get_num(8, '16')

        rc, job = self.provisionv3.break_replication_relationship(
            self.conn, repServiceInstanceName, syncInstanceName,
            operation)
        return rc, cloneDict

    def _delete_cg_and_members(
            self, storageSystem, extraSpecs, cgName, modelUpdate, volumes):
        """Helper function to delete a consistency group and its member volumes

        :param storageSystem: storage system
        :param repServiceInstanceName: the replication service
        :param cgInstanceName: consistency group instance name
        """
        replicationService = self.utils.find_replication_service(
            self.conn, storageSystem)

        storageConfigservice = (
            self.utils.find_storage_configuration_service(
                self.conn, storageSystem))
        cgInstanceName = self._find_consistency_group(
            replicationService, cgName)
        if cgInstanceName is None:
            exception_message = (_("Cannot find CG group %s."), cgName)
            raise exception.VolumeBackendAPIException(
                data=exception_message)
        memberInstanceNames = self._get_members_of_replication_group(
            cgInstanceName)

        self.provision.delete_consistency_group(
            self.conn, replicationService, cgInstanceName, cgName)

        if memberInstanceNames:
            try:
                controllerConfigurationService = (
                    self.utils.find_controller_configuration_service(
                        self.conn, storageSystem))
                for memberInstanceName in memberInstanceNames:
                    self._pre_check_for_deletion(
                        controllerConfigurationService,
                        memberInstanceName, 'Member Volume')
                LOG.debug("Deleting CG members. CG: %(cg)s "
                          "%(numVols)lu member volumes: %(memVols)s",
                          {'cg': cgInstanceName,
                           'numVols': len(memberInstanceNames),
                           'memVols': memberInstanceNames})
                if extraSpecs[ISV3]:
                    self.provisionv3.delete_volume_from_pool(
                        self.conn, storageConfigservice,
                        memberInstanceNames, None)
                else:
                    self.provision.delete_volume_from_pool(
                        self.conn, storageConfigservice,
                        memberInstanceNames, None)
                    for volumeRef in volumes:
                            volumeRef['status'] = 'deleted'
            except Exception:
                for volumeRef in volumes:
                    volumeRef['status'] = 'error_deleting'
                    modelUpdate['status'] = 'error_deleting'
        return modelUpdate, volumes
