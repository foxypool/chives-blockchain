import asyncio
import logging
import time
from asyncio import sleep
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from blspy import G1Element

import chives.server.ws_connection as ws  # lgtm [py/import-and-import-from]
from chives.consensus.coinbase import create_puzzlehash_for_pk
from chives.consensus.constants import ConsensusConstants
from chives.consensus.pot_iterations import calculate_sp_interval_iters
from chives.farmer.pooling.og_pool_state import OgPoolState
from chives.farmer.pooling.pool_api_client import PoolApiClient
from chives.protocols import farmer_protocol, harvester_protocol
from chives.protocols.protocol_message_types import ProtocolMessageTypes
from chives.server.outbound_message import NodeType, make_msg
from chives.server.ws_connection import WSChivesConnection
from chives.types.blockchain_format.proof_of_space import ProofOfSpace
from chives.types.blockchain_format.sized_bytes import bytes32
from chives.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chives.util.config import load_config, save_config
from chives.util.ints import uint32, uint64
from chives.util.keychain import Keychain
from chives.wallet.derive_keys import master_sk_to_farmer_sk, master_sk_to_pool_sk, master_sk_to_wallet_sk

log = logging.getLogger(__name__)


"""
HARVESTER PROTOCOL (FARMER <-> HARVESTER)
"""


class Farmer:
    def __init__(
        self,
        root_path: Path,
        farmer_config: Dict,
        pool_config: Dict,
        keychain: Keychain,
        consensus_constants: ConsensusConstants,
    ):
        self._root_path = root_path
        self.config = farmer_config
        # Keep track of all sps, keyed on challenge chain signage point hash
        self.sps: Dict[bytes32, List[farmer_protocol.NewSignagePoint]] = {}

        # Keep track of harvester plot identifier (str), target sp index, and PoSpace for each challenge
        self.proofs_of_space: Dict[bytes32, List[Tuple[str, ProofOfSpace]]] = {}

        # Quality string to plot identifier and challenge_hash, for use with harvester.RequestSignatures
        self.quality_str_to_identifiers: Dict[bytes32, Tuple[str, bytes32, bytes32, bytes32]] = {}

        # number of responses to each signage point
        self.number_of_responses: Dict[bytes32, int] = {}

        # A dictionary of keys to time added. These keys refer to keys in the above 4 dictionaries. This is used
        # to periodically clear the memory
        self.cache_add_time: Dict[bytes32, uint64] = {}

        self.cache_clear_task: asyncio.Task
        self.constants = consensus_constants
        self._shut_down = False
        self.server: Any = None
        self.keychain = keychain
        self.state_changed_callback: Optional[Callable] = None
        self.log = log
        all_sks = self.keychain.get_all_private_keys()
        self._private_keys = [master_sk_to_farmer_sk(sk) for sk, _ in all_sks] + [
            master_sk_to_pool_sk(sk) for sk, _ in all_sks
        ]

        if len(self.get_public_keys()) == 0:
            error_str = "No keys exist. Please run 'chives keys generate' or open the UI."
            raise RuntimeError(error_str)

        # This is the farmer configuration
        self.farmer_target_encoded = self.config["xcc_target_address"]
        self.farmer_target = decode_puzzle_hash(self.farmer_target_encoded)
        
        self.community_target = self.constants.GENESIS_PRE_FARM_COMMUNITY_PUZZLE_HASH

        self.pool_public_keys = [G1Element.from_bytes(bytes.fromhex(pk)) for pk in self.config["pool_public_keys"]]

        # This is the pool configuration, which should be moved out to the pool once it exists
        self.pool_target_encoded = pool_config["xcc_target_address"]
        self.pool_target = decode_puzzle_hash(self.pool_target_encoded)
        self.pool_sks_map: Dict = {}
        for key in self.get_private_keys():
            self.pool_sks_map[bytes(key.get_g1())] = key

        assert len(self.farmer_target) == 32
        assert len(self.pool_target) == 32
        if len(self.pool_sks_map) == 0:
            error_str = "No keys exist. Please run 'chives keys generate' or open the UI."
            raise RuntimeError(error_str)

        # OG Pooling setup
        self.pool_url = self.config.get("pool_url")
        self.pool_payout_address = self.config.get("pool_payout_address")
        self.pool_sub_slot_iters = self.constants.POOL_SUB_SLOT_ITERS
        self.iters_limit = calculate_sp_interval_iters(self.constants, self.pool_sub_slot_iters)
        self.pool_minimum_difficulty: uint64 = uint64(1)
        self.og_pool_state: OgPoolState = OgPoolState(difficulty=self.pool_minimum_difficulty)
        self.pool_var_diff_target_in_seconds = 5 * 60
        self.pool_reward_target = self.pool_target
        self.adjust_pool_difficulties_task: Optional[asyncio.Task] = None
        self.check_pool_reward_target_task: Optional[asyncio.Task] = None

    def is_pooling_enabled(self):
        return self.pool_url is not None and self.pool_payout_address is not None

    async def _start(self):
        self.cache_clear_task = asyncio.create_task(self._periodically_clear_cache_and_refresh_task())
        if not self.is_pooling_enabled():
            self.log.info(f"Not OG pooling as 'pool_payout_address' and/or 'pool_url' are missing in your config")
            return
        self.pool_api_client = PoolApiClient(self.pool_url)
        await self.initialize_pooling()
        self.adjust_pool_difficulty_task = asyncio.create_task(self._periodically_adjust_pool_difficulty_task())
        self.check_pool_reward_target_task = asyncio.create_task(self._periodically_check_pool_reward_target_task())

    async def initialize_pooling(self):
        pool_info: Dict = {}
        has_pool_info = False
        while not has_pool_info:
            try:
                pool_info = await self.pool_api_client.get_pool_info()
                has_pool_info = True
            except Exception as e:
                self.log.error(f"Error retrieving OG pool info: {e}")
                await sleep(5)

        pool_name = pool_info["name"]
        self.log.info(f"Connected to OG pool {pool_name}")
        self.pool_var_diff_target_in_seconds = pool_info["var_diff_target_in_seconds"]

        self.pool_minimum_difficulty = uint64(pool_info["minimum_difficulty"])
        self.og_pool_state.difficulty = self.pool_minimum_difficulty

        pool_target = bytes.fromhex(pool_info["target_puzzle_hash"][2:])
        assert len(pool_target) == 32
        self.pool_reward_target = pool_target
        address_prefix = self.config["network_overrides"]["config"][self.config["selected_network"]][
            "address_prefix"]
        pool_target_encoded = encode_puzzle_hash(pool_target, address_prefix)
        if self.pool_target is not pool_target or self.pool_target_encoded is not pool_target_encoded:
            self.set_reward_targets(farmer_target_encoded=None, pool_target_encoded=pool_target_encoded)

    def _close(self):
        self._shut_down = True

    async def _await_closed(self):
        await self.cache_clear_task
        if self.adjust_pool_difficulty_task is not None:
            await self.adjust_pool_difficulty_task
        if self.check_pool_reward_target_task is not None:
            await self.check_pool_reward_target_task

    def _set_state_changed_callback(self, callback: Callable):
        self.state_changed_callback = callback

    async def on_connect(self, peer: WSChivesConnection):
        # Sends a handshake to the harvester
        self.state_changed("add_connection", {})
        handshake = harvester_protocol.HarvesterHandshake(
            self.get_public_keys(),
            self.pool_public_keys,
        )
        if peer.connection_type is NodeType.HARVESTER:
            msg = make_msg(ProtocolMessageTypes.harvester_handshake, handshake)
            await peer.send_message(msg)

    def set_server(self, server):
        self.server = server

    def state_changed(self, change: str, data: Dict[str, Any]):
        if self.state_changed_callback is not None:
            self.state_changed_callback(change, data)

    def on_disconnect(self, connection: ws.WSChivesConnection):
        self.log.info(f"peer disconnected {connection.get_peer_info()}")
        self.state_changed("close_connection", {})

    def get_public_keys(self):
        return [child_sk.get_g1() for child_sk in self._private_keys]

    def get_private_keys(self):
        return self._private_keys

    def get_reward_targets(self, search_for_private_key: bool) -> Dict:
        if search_for_private_key:
            all_sks = self.keychain.get_all_private_keys()
            stop_searching_for_farmer, stop_searching_for_pool = False, False
            for i in range(500):
                if stop_searching_for_farmer and stop_searching_for_pool and i > 0:
                    break
                for sk, _ in all_sks:
                    ph = create_puzzlehash_for_pk(master_sk_to_wallet_sk(sk, uint32(i)).get_g1())

                    if ph == self.farmer_target:
                        stop_searching_for_farmer = True
                    if ph == self.pool_target:
                        stop_searching_for_pool = True
            return {
                "farmer_target": self.farmer_target_encoded,
                "pool_target": self.pool_target_encoded,
                "have_farmer_sk": stop_searching_for_farmer,
                "have_pool_sk": stop_searching_for_pool,
            }
        return {
            "farmer_target": self.farmer_target_encoded,
            "pool_target": self.pool_target_encoded,
        }

    def set_reward_targets(self, farmer_target_encoded: Optional[str], pool_target_encoded: Optional[str]):
        config = load_config(self._root_path, "config.yaml")
        if farmer_target_encoded is not None:
            self.farmer_target_encoded = farmer_target_encoded
            self.farmer_target = decode_puzzle_hash(farmer_target_encoded)
            config["farmer"]["xcc_target_address"] = farmer_target_encoded
        if pool_target_encoded is not None:
            self.pool_target_encoded = pool_target_encoded
            self.pool_target = decode_puzzle_hash(pool_target_encoded)
            config["pool"]["xcc_target_address"] = pool_target_encoded
        save_config(self._root_path, "config.yaml", config)

    async def _periodically_clear_cache_and_refresh_task(self):
        time_slept: uint64 = uint64(0)
        refresh_slept = 0
        while not self._shut_down:
            if time_slept > self.constants.SUB_SLOT_TIME_TARGET:
                now = time.time()
                removed_keys: List[bytes32] = []
                for key, add_time in self.cache_add_time.items():
                    if now - float(add_time) > self.constants.SUB_SLOT_TIME_TARGET * 3:
                        self.sps.pop(key, None)
                        self.proofs_of_space.pop(key, None)
                        self.quality_str_to_identifiers.pop(key, None)
                        self.number_of_responses.pop(key, None)
                        removed_keys.append(key)
                for key in removed_keys:
                    self.cache_add_time.pop(key, None)
                time_slept = uint64(0)
                log.debug(
                    f"Cleared farmer cache. Num sps: {len(self.sps)} {len(self.proofs_of_space)} "
                    f"{len(self.quality_str_to_identifiers)} {len(self.number_of_responses)}"
                )
            time_slept += 1
            refresh_slept += 1
            # Periodically refresh GUI to show the correct download/upload rate.
            if refresh_slept >= 30:
                self.state_changed("add_connection", {})
                refresh_slept = 0
            await asyncio.sleep(1)

    async def _periodically_adjust_pool_difficulty_task(self):
        time_slept = 0
        while not self._shut_down:
            # Sleep in 1 sec intervals to quickly exit outer loop, but effectively sleep 60 sec between actual code runs
            await sleep(1)
            time_slept += 1
            if time_slept < 60:
                continue
            time_slept = 0
            if (time.time() - self.og_pool_state.last_partial_submit_timestamp) < self.pool_var_diff_target_in_seconds:
                continue
            diff_since_last_partial_submit_in_seconds = time.time() - self.og_pool_state.last_partial_submit_timestamp
            missing_partial_submits = int(
                diff_since_last_partial_submit_in_seconds // self.pool_var_diff_target_in_seconds)
            new_difficulty = uint64(max(
                (self.og_pool_state.difficulty - (missing_partial_submits * 2)),
                self.pool_minimum_difficulty
            ))
            if new_difficulty == self.og_pool_state.difficulty:
                continue
            old_difficulty = self.og_pool_state.difficulty
            self.og_pool_state.difficulty = new_difficulty
            log.info(
                f"Lowered the OG pool difficulty from {old_difficulty} to "
                f"{new_difficulty} due to no partial submits within the last "
                f"{int(round(diff_since_last_partial_submit_in_seconds))} seconds"
            )

    async def _periodically_check_pool_reward_target_task(self):
        time_slept = 0
        while not self._shut_down:
            # Sleep in 1 sec intervals to quickly exit outer loop, but effectively sleep 5 min between actual code runs
            await sleep(1)
            time_slept += 1
            if time_slept < 5 * 60:
                continue
            time_slept = 0
            if self.pool_target is self.pool_reward_target:
                continue
            address_prefix = self.config["network_overrides"]["config"][self.config["selected_network"]]["address_prefix"]
            pool_target_encoded = encode_puzzle_hash(self.pool_reward_target, address_prefix)
            self.set_reward_targets(farmer_target_encoded=None, pool_target_encoded=pool_target_encoded)