"""Independent OPC HBX-4 instrument adapter."""

from instruments.opc.shared_adapter import OpcAdapterBase


class OpcHbx4Adapter(OpcAdapterBase):
    instrument_id = "opc_hbx4"
    display_name = "OPC HBX-4"


__all__ = ["OpcHbx4Adapter"]
