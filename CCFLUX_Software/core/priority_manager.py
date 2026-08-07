"""Default CCFLUX processing queue definitions."""

from __future__ import annotations

from .processing_manager import ProcessingJob, ProcessingPriorityQueue, WorkerGroup


# The campaign's processing order:
#
#   Noseboom, MIRO, Picarro, OPC HBX-4/5, Partector Pro, INS Gimbal, SIF,
#   FLIR, GoPro, MicaSense
#
# This tuple is that order. A job is dispatched on (priority, order), so within
# a worker group the position here decides which starts first, and only the
# instruments the operator selected are dispatched at all - an unselected one is
# skipped without changing the relative order of the rest.
#
# The three cameras therefore share one priority: theirs is a single-worker
# group, so they run strictly in this sequence, and a priority that disagreed
# with the position would silently outrank it. GoPro used to be priority 3,
# which put it last however it was ordered here.
#
# INS Gimbal is not named in the campaign order; it sits with the other Hatchbox
# instruments, after Partector Pro.
#
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
    ("flir_quick", "flir", "FLIR metadata quick check", WorkerGroup.CAMERA_METADATA, 2, False, False),
    ("gopro_quick", "gopro", "GoPro metadata quick check", WorkerGroup.CAMERA_METADATA, 2, False, False),
    ("micasense_quick", "micasense", "MicaSense metadata quick check", WorkerGroup.CAMERA_METADATA, 2, False, False),
)

# The same order, by instrument, for everything that presents or dispatches the
# instruments outside the queue. Kept beside the queue so the two cannot drift.
CAMPAIGN_PROCESSING_ORDER = (
    "noseboom", "miro", "picarro", "opc_hbx4", "opc_hbx5",
    "partector", "ins_gimbal", "sif", "flir", "gopro", "micasense",
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
