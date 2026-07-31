"""Independent OPC HBX-5 instrument adapter."""

from instruments.opc.shared_adapter import OpcAdapterBase


class OpcHbx5Adapter(OpcAdapterBase):
    instrument_id = "opc_hbx5"
    display_name = "OPC HBX-5"


__all__ = ["OpcHbx5Adapter"]
