import os
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3


class ReliableUDPController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    PRIMARY = {1: {1: 2, 2: 1}, 2: {1: 2, 2: 1}, 3: {}, 4: {1: 2, 2: 1}}
    BACKUP = {1: {1: 3, 3: 1}, 2: {}, 3: {1: 2, 2: 1}, 4: {1: 3, 3: 1}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.failed_ports = set()
        self.active_path = "PRIMARY"
        self.mode = os.environ.get("SDN_MODE", "adaptive").lower()
        if self.mode not in ("adaptive", "static"):
            raise ValueError("SDN_MODE must be adaptive or static")
        self.logger.info("SDN MODE: %s", self.mode.upper())

    def install_flow(self, datapath, in_port, out_port):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        flow = parser.OFPFlowMod(
            datapath=datapath, priority=100, match=parser.OFPMatch(in_port=in_port),
            instructions=[parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS, [parser.OFPActionOutput(out_port)])])
        datapath.send_msg(flow)

    def configure_path(self, name):
        rules = self.PRIMARY if name == "PRIMARY" else self.BACKUP
        for dpid, datapath in self.datapaths.items():
            ofp, parser = datapath.ofproto, datapath.ofproto_parser
            datapath.send_msg(parser.OFPFlowMod(
                datapath=datapath, command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                table_id=0, match=parser.OFPMatch()))
            for in_port, out_port in rules.get(dpid, {}).items():
                self.install_flow(datapath, in_port, out_port)
        self.active_path = name
        self.logger.info("ACTIVE SDN PATH: %s", name)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self.logger.info("Switch S%s connected", dp.id)
        rules = self.PRIMARY if self.active_path == "PRIMARY" else self.BACKUP
        for in_port, out_port in rules.get(dp.id, {}).items():
            self.install_flow(dp, in_port, out_port)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        dpid, port = msg.datapath.id, msg.desc.port_no
        if (dpid, port) not in {(2, 2), (4, 2)}:
            return
        ofp = msg.datapath.ofproto
        down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN or
                    msg.desc.config & ofp.OFPPC_PORT_DOWN)
        if down:
            self.failed_ports.add((dpid, port))
            self.logger.warning("PRIMARY LINK FAILURE: S%s port %s", dpid, port)
        else:
            self.failed_ports.discard((dpid, port))
            self.logger.info("PRIMARY LINK RESTORED: S%s port %s", dpid, port)
        if self.mode == "static":
            return
        desired = "BACKUP" if self.failed_ports else "PRIMARY"
        if desired != self.active_path:
            self.configure_path(desired)
