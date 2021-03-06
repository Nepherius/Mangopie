from core.aochat.delay_queue import DelayQueue
from core.aochat import server_packets, client_packets
from core.decorators import instance
from core.aochat.bot import Bot
from core.buddy_manager import BuddyManager
from core.character_manager import CharacterManager
from core.access_manager import AccessManager
from core.setting_manager import SettingManager
from core.public_channel_manager import PublicChannelManager
from tools.text import Text
from tools.chat_blob import ChatBlob
from tools.setting_types import TextSettingType, ColorSettingType, NumberSettingType
from tools.bot_status import BotStatus
import time


@instance()
class Mangopie(Bot):
    def __init__(self):
        super().__init__()
        self.ready = False
        self.packet_handlers = {}
        self.org_id = None
        self.org_name = None
        self.superadmin = None
        self.status: BotStatus = BotStatus.SHUTDOWN
        self.dimension = None
        self.packet_queue = DelayQueue(2, 2.5)
        self.last_timer_event = 0

    def inject(self, registry):
        self.db = registry.get_instance("db")
        self.buddy_manager: BuddyManager = registry.get_instance("buddy_manager")
        self.character_manager: CharacterManager = registry.get_instance("character_manager")
        self.setting_manager: SettingManager = registry.get_instance("setting_manager")
        self.access_manager: AccessManager = registry.get_instance("access_manager")
        self.command_manager = registry.get_instance("command_manager")
        self.public_channel_manager: PublicChannelManager = registry.get_instance("public_channel_manager")
        self.text: Text = registry.get_instance("text")
        self.pork_manager = registry.get_instance("pork_manager")
        self.event_manager = registry.get_instance("event_manager")
        self.job_scheduler = registry.get_instance("job_scheduler")
        self.mmdb = registry.get_instance("mmdb_parser")

    def init(self, config, registry):
        self.superadmin = config["superadmin"].capitalize()
        self.dimension = 5

        # prepare indexes, commands, events, and settings
        self.db.client['admin'].create_index("char_id", unique=True,background=True)
        self.db.client['player'].create_index("char_id", unique=True,background=True)
        self.db.client['online'].create_index("char_id", unique=True,background=True)
        self.db.client['event_config'].create_index("event_type", background=True)
        self.db.client['command_config'].update_many({}, {'$set': {'verified': 0}})
        self.db.client['event_config'].update_many({}, {'$set': {'verified': 0}})
        self.db.client['settings'].update_many({}, {'$set': {'verified': 0}})

        registry.pre_start_all()
        registry.start_all()

        # remove commands, events, and settings that are no longer registered
        self.db.delete_all('settings', {'verified': 0})
        self.db.delete_all('command_config', {'verified': 0})
        self.db.delete_all('event_config', {'verified': 0})
        self.status = BotStatus.RUN

    def post_start(self):
        self.pork_manager.get_character_info(self.superadmin)
        self.ready = True

    def pre_start(self):
        pass
        self.access_manager.register_access_level("superadmin", 10, self.check_superadmin)
        self.event_manager.register_event_type("connect")
        self.event_manager.register_event_type("packet")

    def start(self):
        self.setting_manager.register("org_channel_max_page_length", 7500, "Maximum size of blobs in org channel",
                                      NumberSettingType([4500, 6000, 7500, 9000, 10500, 12000]), "core.system")
        self.setting_manager.register("private_message_max_page_length", 7500,
                                      "Maximum size of blobs in private messages",
                                      NumberSettingType([4500, 6000, 7500, 9000, 10500, 12000]), "core.system", )
        self.setting_manager.register("private_channel_max_page_length", 7500,
                                      "Maximum size of blobs in private channel",
                                      NumberSettingType([4500, 6000, 7500, 9000, 10500, 12000]), "core.system")
        self.setting_manager.register("header_color", "#FFFF00", "color for headers", ColorSettingType(), "core.colors")
        self.setting_manager.register("header2_color", "#FCA712", "color for sub-headers", ColorSettingType(),
                                      "core.colors")
        self.setting_manager.register("highlight_color", "#FFFFFF", "color for highlight", ColorSettingType(),
                                      "core.colors")
        self.setting_manager.register("neutral_color", "#E6E1A6", "color for neutral faction", ColorSettingType(),
                                      "core.colors")
        self.setting_manager.register("omni_color", "#FA8484", "color for omni faction", ColorSettingType(),
                                      "core.colors")
        self.setting_manager.register("clan_color", "#F79410", "color for clan faction", ColorSettingType(),
                                      "core.colors")
        self.setting_manager.register("unknown_color", "#FF0000", "color for unknown faction", ColorSettingType(),
                                      "core.colors")
        self.setting_manager.register("notice_color", "#FF8C00", "color for important notices", ColorSettingType(),
                                      "core.colors")
        self.setting_manager.register("symbol", "!", "Symbol for executing bot commands",
                                      TextSettingType(["!", "#", "*", "@", "$", "+", "-"]), "core.system")

    def check_superadmin(self, char_id):
        char_name = self.character_manager.resolve_char_to_name(char_id)
        return char_name == self.superadmin

    def run(self):
        while None is not self.iterate():
            pass

        self.event_manager.fire_event("connect", None)
        self.post_start()

        while self.status == BotStatus.RUN:
            timestamp = int(time.time())

            # timer events will execute not more often than once per second
            if self.last_timer_event < timestamp:
                self.last_timer_event = timestamp
                self.job_scheduler.check_for_scheduled_jobs(timestamp)
                self.event_manager.check_for_timer_events(timestamp)

            self.iterate()

        return self.status

    def add_packet_handler(self, packet_id, handler):
        handlers = self.packet_handlers.get(packet_id, [])
        handlers.append(handler)
        self.packet_handlers[packet_id] = handlers

    def iterate(self):
        packet = self.read_packet()
        if packet:
            if isinstance(packet, server_packets.PrivateMessage):
                self.handle_private_message(packet)
            elif isinstance(packet, server_packets.PublicChannelJoined):
                # set org id and org name
                if packet.channel_id >> 32 == 3:
                    self.org_id = 0x00ffffffff & packet.channel_id
                    if packet.name != "Clan (name unknown)":
                        self.org_name = packet.name
            elif isinstance(packet, server_packets.SystemMessage):
                category_id = 20000
                instance_id = packet.message_id
                template = self.mmdb.get_message_string(category_id, instance_id)
                params = self.mmdb.parse_params(packet.message_args)
                self.logger.info(template % tuple(params))

            for handler in self.packet_handlers.get(packet.id, []):
                handler(packet)

            self.event_manager.fire_event("packet:" + str(packet.id), packet)

        # check packet queue for outgoing packets
        outgoing_packet = self.packet_queue.dequeue()
        while outgoing_packet:
            self.send_packet(outgoing_packet)
            outgoing_packet = self.packet_queue.dequeue()

        return packet

    def send_org_message(self, msg):
        org_channel_id = self.public_channel_manager.org_channel_id
        if org_channel_id is None:
            self.logger.warning("Could not send message to org channel, unknown org id")
        else:
            for page in self.get_text_pages(msg, self.setting_manager.get("org_channel_max_page_length").get_value()):
                packet = client_packets.PublicChannelMessage(org_channel_id, page, "")
                # self.send_packet(packet)
                self.packet_queue.enqueue(packet)

    def send_private_message(self, char, msg):
        char_id = self.character_manager.resolve_char_to_id(char)
        if char_id is None:
            self.logger.warning("Could not send message to %s, could not find char id" % char)
        else:
            for page in self.get_text_pages(msg,
                                            self.setting_manager.get("private_message_max_page_length").get_value()):
                self.logger.log_tell("To", self.character_manager.get_char_name(char_id), page)
                packet = client_packets.PrivateMessage(char_id, page, "\0")
                # self.send_packet(packet)
                self.packet_queue.enqueue(packet)

    def send_private_channel_message(self, msg, private_channel=None):
        if private_channel is None:
            private_channel = self.char_id

        private_channel_id = self.character_manager.resolve_char_to_id(private_channel)
        if private_channel_id is None:
            self.logger.warning(
                "Could not send message to private channel %s, could not find private channel" % private_channel)
        else:
            for page in self.get_text_pages(msg,
                                            self.setting_manager.get("private_channel_max_page_length").get_value()):
                packet = client_packets.PrivateChannelMessage(private_channel_id, page, "\0")
                self.send_packet(packet)

    def handle_private_message(self, packet: server_packets.PrivateMessage):
        self.logger.log_tell("From", self.character_manager.get_char_name(packet.char_id), packet.message)

    def handle_public_channel_message(self, packet: server_packets.PublicChannelMessage):
        self.logger.log_chat(
            self.public_channel_manager.get_channel_name(packet.channel_id),
            self.character_manager.get_char_name(packet.char_id),
            packet.message)

    def get_text_pages(self, msg, max_page_length):
        if isinstance(msg, ChatBlob):
            return self.text.paginate(msg.title, msg.msg, max_page_length, msg.max_num_pages, msg.footer)
        else:
            return [self.text.format_message(msg)]

    def is_ready(self):
        return self.ready

    def shutdown(self):
        self.status = BotStatus.SHUTDOWN

    def restart(self):
        self.status = BotStatus.RESTART
