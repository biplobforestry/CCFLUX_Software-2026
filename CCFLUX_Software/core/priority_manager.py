"""Default CCFLUX processing queue definitions."""

from __future__ import annotations

from .processing_manager import ProcessingJob, ProcessingPriorityQueue, WorkerGroup


# Camera products are one job each. FLIR used to carry a second "detailed"
# job the operator had to find and start separately, which left the map view
# waiting on work that had not been asked for - a thermal delivery is no use
# without positions, so the conversion and the Noseboom match now run as part
# of the FLIR job itself.
DEFAULT_PROCESSING_JOBS = (
    ("noseboom", "noseboom", "Noseboom", WorkerGroup.FAST_SCIENCE, 1, False, False),
    ("miro", "miro", "MIRO", WorkerGroup.FAST_SCIENCE, 1, False, False),
    # Detection is available, but no independent validated modular adapter exists.
    ("picarro", "picarro", "Picarro", WorkerGroup.FAST_SCIENCE, 1, False, False),
    ("opc_hbx4", "opc_hbx4", "OPC HBX-4", WorkerGroup.FAST_SCIENCE, 1, False, False),
    ("opc_hbx5", "opc_hbx5", "OPC HBX-5", WorkerGroup.FAST_SCIENCE, 1, False, False),
    ("partector", "partector", "Partector Pro", WorkerGroup.FAST_SCIENCE, 1, False, False),
    ("ins_gimbal", "ins_gimbal", "INS Gimbal", WorkerGroup.FAST_SCIENCE, 1, False, False),
    ("sif", "sif", "SIF", WorkerGroup.FAST_SCIENCE, 2, False, False),
    ("micasense_quick", "micasense", "MicaSense metadata quick check", WorkerGroup.CAMERA_METADATA, 2, False, False),
    ("flir_quick", "flir", "FLIR metadata quick check", WorkerGroup.CAMERA_METADATA, 2, False, False),
    ("gopro_quick", "gopro", "GoPro metadata quick check", WorkerGroup.CAMERA_METADATA, 3, False, False),
)


def create_default_priority_queue() -> ProcessingPriorityQueue:
    queue = ProcessingPriorityQueue()
    for (
        job_id,
        instrument_id,
        display_name,
        worker_group,
        priority,
        enabled,
        detailed,
    ) in DEFAULT_PROCESSING_JOBS:
        queue.add(
            ProcessingJob(
                job_id=job_id,
                instrument_id=instrument_id,
                display_name=display_name,
                worker_group=worker_group,
                priority=priority,
                enabled=enabled,
                detailed=detailed,
            )
        )
    return queue
