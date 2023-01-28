import skip
import os
import json
import logging
import ast
import math
import time
import logging
import httpx

from dotenv import load_dotenv
from hashlib import sha256
from base64 import b64decode
from dataclasses import dataclass   
from cosmpy.aerial.client import LedgerClient, NetworkConfig

from src.decoder import Decoder
from src.querier import Querier
from src.executor import Executor
from src.creator import Creator
from src.state import State
from src.transaction import Transaction
from src.contract import Pool
from src.route import Route


DELAY_BETWEEN_SENDS = 0.5
DESIRED_HEIGHT = 0
SYNC = True
READ_TIMEOUT = 10
SUCCESS_CODE = 0
RETRY_FAILURE_CODES = [4, 8]
NOT_A_SKIP_VAL_CODE = 4


@dataclass
class Bot:
    """ This class holds all the bot configuration and state.
        It is used to create a bot instance and run the bot.
    """
    env_file_path: str
    
    async def init(self):
        # Load environment variables
        load_dotenv(self.env_file_path)
        # Set file paths
        self.log_file: str = os.environ.get("LOG_FILE")
        self.contracts_file: str = os.environ.get("CONTRACTS_FILE")
        # Set general chain / settings variables
        self.mnemonic: str = os.environ.get("MNEMONIC")
        self.rpc_url: str = os.environ.get("RPC_URL")
        self.rest_url: str = os.environ.get("REST_URL")
        self.chain_id: str = os.environ.get("CHAIN_ID")
        self.fee_denom: str = os.environ.get("FEE_DENOM")
        self.gas_limit: int = int(os.environ.get("GAS_LIMIT"))
        self.gas_price: float = float(os.environ.get("GAS_PRICE"))
        self.gas_fee: int = int(self.gas_limit * self.gas_price)
        self.fee: str = f"{self.gas_fee}{self.fee_denom}"
        self.arb_denom: str = os.environ.get("ARB_DENOM")
        self.address_prefix: str = os.environ.get("ADDRESS_PREFIX")
        # Set Skip variables
        self.skip_rpc_url: str = os.environ.get("SKIP_RPC_URL")
        self.auction_house_address: str = os.environ.get("AUCTION_HOUSE_ADDRESS")
        self.auction_bid_profit_percentage: float = float(os.environ.get("AUCTION_BID_PROFIT_PERCENTAGE"))
        self.auction_bid_minimum: int = int(os.environ.get("AUCTION_BID_MINIMUM"))
        # Create and set Queryer and Decoder
        self.creator: Creator = Creator()
        self.querier: Querier = self.creator.create_querier(
                                        querier=os.environ.get("QUERIER"), 
                                        rpc_url=self.rpc_url)
        self.decoder: Decoder = self.creator.create_decoder(decoder=os.environ.get("DECODER"))
        self.executor: Executor = self.creator.create_executor(executor=os.environ.get("EXECUTOR"))
        # Set factory and router contracts
        self.factory_contracts: dict = ast.literal_eval(os.environ.get("FACTORY_CONTRACTS"))
        self.router_contracts: dict = ast.literal_eval(os.environ.get("ROUTER_CONTRACTS"))
        # Set defaults for the bot
        self.reset: bool = True
        self.account_balance: int = 0
        # Set up logging
        logging.basicConfig(filename=os.environ.get("LOG_FILE"), 
                            encoding='utf-8', 
                            level=logging.INFO)
        logging.getLogger().addHandler(logging.StreamHandler())
        # Create and set client and wallet
        self.network_config = NetworkConfig(
                                    chain_id=self.chain_id,
                                    url=f"rest+{self.rest_url}",
                                    fee_minimum_gas_price=self.gas_price,
                                    fee_denomination=self.fee_denom,
                                    staking_denomination=self.fee_denom,
                                    )
        self.client = LedgerClient(self.network_config)
        self.wallet = self.creator.create_wallet(self.chain_id, 
                                                 self.mnemonic, 
                                                 self.address_prefix) 
        # Get any existing contracts from the contracts file
        with open(self.contracts_file) as f:
            self.init_contracts: dict = json.load(f)
        # Initialize the state
        self.state: State = State()
        # Update all pool contracts in state
        print("Updating all pool contracts in state...")
        await self.state.set_all_pool_contracts(
                                init_contracts=self.init_contracts,
                                querier=self.querier,
                                creator=self.creator,
                                factory_contracts=self.factory_contracts,
                                arb_denom=self.arb_denom
                                )
        # Get list of all contract addresses
        self.contract_list: list = list(self.state.contracts.keys())
        
        # Update contracts file after init
        # @USER TODO: Uncomment this line if you want to update the contracts file after init
        #self._update_contracts_file()
            
    def _update_contracts_file(self):
        dict_pools = {}
        for contract in self.state.contracts:
            dict_pools[contract] = self.state.contracts[contract].__dict__
            
        with open(self.contracts_file, 'w') as f:
            json.dump(dict_pools, f, indent=4)
    
    async def run(self):
        print("Scanning Mempool...")
        while True:
            # Update the account balance
            if self.reset:
                account_balance, reset = self.querier.update_account_balance(
                                                            client=self.client,
                                                            wallet=self.wallet,
                                                            denom=self.arb_denom,
                                                            network_config=self.network_config
                                                            )
                if not reset:
                    self.reset = reset
                    self.account_balance = account_balance
            # Query the mempool for new transactions, returns once new txs are found
            backrun_list = self.querier.query_node_for_new_mempool_txs()
            #print(f"{time.time()}: Found new transactions in mempool")
            start = time.time()
            # Set flag to update reserves once per potential backrun list
            new_backrun_list = True
            # Iterate through each tx and assess for profitable opportunities
            for tx_str in backrun_list:
                # Create a transaction object
                tx_hash = sha256(b64decode(tx_str)).digest().hex()
                logging.info(f"Iterating transaction {tx_hash}")
                transaction: Transaction = Transaction(contracts=self.state.contracts, 
                                                       tx_str=tx_str, 
                                                       decoder=self.decoder,
                                                       arb_denom=self.arb_denom)
                # If there are no swaps, continue
                if not transaction.swaps:
                    continue
                # Update reserves once per potential backrun list
                if new_backrun_list:
                    print("Updating all reserves...")
                    start_update = time.time()
                    await self.state.update_all(jobs=self.state.update_all_reserves_jobs)
                    end_update = time.time()
                    logging.info(f"Time to update all reserves: {end_update - start_update}")
                    new_backrun_list = False
                # Simulate the transaction on a copy of contract state 
                # and return the copied state post-transaction simulation
                contracts_copy = self.state.simulate_transaction(transaction=transaction)
                # Add routes to the transaction after simulation
                transaction.add_routes(
                                contracts=contracts_copy,
                                arb_denom=self.arb_denom
                                )
                # If there are no routes, continue
                if not transaction.routes:
                    continue
                # Build the most profitable bundle from 
                bundle: list = self.build_most_profitable_bundle(
                                                transaction=transaction,
                                                contracts=contracts_copy
                                                )
                # If there is a profitable bundle, fire away!
                end = time.time()
                logging.info(f"Time from seeing {tx_hash} in mempool and building bundle if exists: {end - start}")
                if bundle and bundle[-1] is not None:
                    self.fire(bundle=bundle)
                    
    def build_most_profitable_bundle(self,
                                     transaction: Transaction,
                                     contracts: dict[str, Pool]) -> list[bytes | None]:
        """ Build backrun bundle for transaction"""
        # Calculate the profit for each route
        for route in transaction.routes:
            route.calculate_and_set_optimal_amount_in()
            route.calculate_and_set_amount_in(
                            account_balance=self.account_balance,
                            gas_fee=self.gas_fee
                            ) 
            route.calculate_and_set_profit()
            
        transaction.routes.sort(
                        key=lambda route: route.profit, 
                        reverse=True)
        
        highest_profit_route: Route = transaction.routes[0]
        
        if (highest_profit_route.profit <= 0 
            or highest_profit_route.profit <= self.gas_fee):
            logging.info(f"No profitable routes found above gas fee")
            return []
        
        bid = math.floor((highest_profit_route.profit - self.gas_fee) 
                         * self.auction_bid_profit_percentage)
        
        if bid <= self.auction_bid_minimum:
            logging.info(f"No profitable routes found above minimum bid")
            return []
        
        logging.info(f"Arbitrage opportunity found!")
        logging.info(f"Optimal amount in: {highest_profit_route.optimal_amount_in}")
        logging.info(f"Amount in: {highest_profit_route.amount_in}")
        logging.info(f"Profit: {highest_profit_route.profit}")
        logging.info(f"Bid: {bid}")
        logging.info(f"Tx Hash: {sha256(b64decode(transaction.tx_str)).hexdigest()}")
        
        return [transaction.tx_bytes, 
                self.executor.build_backrun_tx(
                                    wallet=self.wallet,
                                    client=self.client,
                                    account_balance=self.account_balance,
                                    auction_house_address=self.auction_house_address,
                                    fee_denom=self.fee_denom,
                                    fee=self.fee,
                                    gas_limit=self.gas_limit,
                                    route=highest_profit_route,
                                    bid=bid,
                                    chain_id=self.chain_id)]
        
    def fire(self, bundle: list[bytes]) -> bool:
        """ Signs and sends the bundle to the Skip auction.
            Retrying and handling errors as necessary.
        """
        try:
            # Use the skip-python helper library to sign and send the bundle
            # For more information on the skip-python library, check out:
            # https://github.com/skip-mev/skip-py
            response = skip.sign_and_send_bundle(
                                bundle=bundle,
                                private_key=self.wallet.signer().private_key_bytes,
                                public_key=self.wallet.signer().public_key,
                                rpc_url=self.skip_rpc_url,
                                desired_height=DESIRED_HEIGHT,
                                sync=SYNC,
                                timeout=READ_TIMEOUT)
            
            # put response into variables for use in telegram and logging
            term_Out = response.json()
            
            code = term_Out['result']['code']
            txs = term_Out['result']['txs']
            auction_fee = term_Out['result']['auction_fee']
            bundle_size = term_Out['result']['bundle_size']
            desired_height = term_Out['result']['desired_height']
            waited_for_sim_results = term_Out['result']['waited_for_simulation_results']
            sim_success = term_Out['result']['simulation_success']
            result_chk_txs = term_Out['result']['result_check_txs']
            result_del_txs = term_Out['result']['result_deliver_txs']
            error = term_Out['result']['error']

            logging.info("----- Checking Possible Arbitrage  -----")
            logging.info(f"Code: {code} Transaction: {txs}")
            logging.info(f"Auction Fee: {auction_fee} Bundle Size: {bundle_size}")
            logging.info(f"Desired Height: {desired_height}")
            logging.info(f"Waited for results: {waited_for_sim_results}")
            logging.info(f"Simulation Success: {sim_success}")
            if result_chk_txs is not None:
                for check_txs in result_chk_txs:
                    # print(check_txs.keys())
                    ctx_code = check_txs['code']
                    ctx_data = check_txs['data']
                    ctx_log = check_txs['log']
                    ctx_info = check_txs['info']
                    ctx_gas_wanted = check_txs['gas_wanted']
                    ctx_gas_used = check_txs['gas_used']
                    ctx_events = check_txs['events']
                    ctx_codespace = check_txs['codespace']

                    logging.info("-----  Checking the Transactions   -----")
                    # logging.info(f"Checked TXs: {check_txs}")
                    logging.info(f"Code: {ctx_code}")
                    logging.info(f"Data: {ctx_data}")
                    logging.info(f"Log: {ctx_log}")  
                    logging.info(f"Info: {ctx_info}") 
                    logging.info(f"Gas Wanted: {ctx_gas_wanted} Gas Used: {ctx_gas_used}")
                    logging.info(f"Events: {ctx_events}")
                    logging.info(f"Codespace: {ctx_codespace}") 
 
            else:
                None    
            if result_del_txs is not None:          
                for del_txs in result_del_txs:
                    # print(del_txs.keys())
                    logging.info("----- Firing Tx(s) to the Chain!!! -----")
                    # logging.info(f"Delivered TXs: {del_txs}")
                    dtx_code = del_txs['code']
                    dtx_data = del_txs['data']
                    dtx_log = del_txs['log']
                    dtx_info = del_txs['info']
                    dtx_gas_wanted = del_txs['gas_wanted']
                    dtx_gas_used = del_txs['gas_used']
                    dtx_events = del_txs['events']
                    dtx_codespace = del_txs['codespace']

                    logging.info(f"Code: {dtx_code}")
                    logging.info(f"Data: {dtx_data}")
                    logging.info(f"Log: {dtx_log}")  
                    logging.info(f"Info: {dtx_info}") 
                    logging.info(f"Gas Wanted: {dtx_gas_wanted} Gas Used: {dtx_gas_used}")
                    logging.info(f"Events: {dtx_events}")
                    logging.info(f"Codespace: {dtx_codespace}") 
            else:
                None 
            logging.info(f"Process Error(s): {error}")
            # logging.info(response.json())
            #logging.info(f"Route and reserves: {route_obj.__dict__}")
        except httpx.ReadTimeout:
            logging.error("Read timeout while waiting for response from Skip")
            return False

        # Check the error code from the response returned by Skip
        # For more information on error codes, check out:
        # https://skip-protocol.notion.site/Skip-Searcher-Documentation-0af486e8dccb4081bdb0451fe9538c99
        if response.json()["result"]["code"] == SUCCESS_CODE:
            self.reset = True
            logging.info("Simulation successful!")
            return True
        # Retry if we get a not a skip val or a deliver tx failure
        if response.json()["result"]["code"] in RETRY_FAILURE_CODES:
            # Keep sending the bundles until we get a success or deliver tx failure
            return self._keep_retrying(bundle=bundle)
        return False
            
    def _keep_retrying(self, bundle: list[bytes]) -> bool:
        """ Keeps sending the bundle until we get a success or deliver tx failure"""
        base64_encoded_bundle, bundle_signature = skip.sign_bundle(
                                                        bundle=bundle[1:],
                                                        private_key=self.wallet.signer().private_key_bytes
                                                        )
        retry_response = self._retry(base64_encoded_bundle, bundle_signature)
        while retry_response is None:
            retry_response = self._retry(base64_encoded_bundle, bundle_signature)
        return retry_response
            
    def _retry(self, base64_encoded_bundle, bundle_signature) -> bool:
        """ Signs and sends the bundle to the Skip auction.
            Retrying and handling errors as necessary.
        """
        try:
            response = skip.send_bundle(
                                b64_encoded_signed_bundle=base64_encoded_bundle,
                                bundle_signature=bundle_signature,
                                public_key=self.wallet.signer().public_key,
                                rpc_url=self.skip_rpc_url,
                                desired_height=DESIRED_HEIGHT,
                                sync=SYNC
                                )
            
            retry_Out = response.json()
            
            rt_code = retry_Out['result']['code']
            rt_txs = retry_Out['result']['txs']
            rt_auction_fee = retry_Out['result']['auction_fee']
            rt_bundle_size = retry_Out['result']['bundle_size']
            rt_desired_height = retry_Out['result']['desired_height']
            rt_waited_for_sim_results = retry_Out['result']['waited_for_simulation_results']
            rt_sim_success = retry_Out['result']['simulation_success']
            rt_result_chk_txs = retry_Out['result']['result_check_txs']
            rt_result_del_txs = retry_Out['result']['result_deliver_txs']
            rt_error = retry_Out['result']['error']
            logging.info("-----        Retrying Bundle       -----")
            logging.info("----- Checking Possible Arbitrage  -----")
            logging.info(f"Code: {rt_code} Transaction: {rt_txs}")
            logging.info(f"Auction Fee: {rt_auction_fee} Bundle Size: {rt_bundle_size}") 
            logging.info(f"Desired Height: {rt_desired_height}")
            logging.info(f"Waited for results: {rt_waited_for_sim_results}")
            logging.info(f"Simulation Success: {rt_sim_success}")

            if rt_result_chk_txs is not None:
                for check_txs in rt_result_chk_txs:
                    # print(check_txs.keys())
                    ctx_code = check_txs['code']
                    ctx_data = check_txs['data']
                    ctx_log = check_txs['log']
                    ctx_info = check_txs['info']
                    ctx_gas_wanted = check_txs['gas_wanted']
                    ctx_gas_used = check_txs['gas_used']
                    ctx_events = check_txs['events']
                    ctx_codespace = check_txs['codespace']
                    logging.info("-----  Checking the Transactions   -----")
                    # logging.info(f"Checked TXs: {check_txs}")
                    logging.info(f"Code: {ctx_code}")
                    logging.info(f"Data: {ctx_data}")
                    logging.info(f"Log: {ctx_log}")  
                    logging.info(f"Info: {ctx_info}") 
                    logging.info(f"Gas Wanted: {ctx_gas_wanted} Gas Used: {ctx_gas_used}")
                    logging.info(f"Events: {ctx_events}")
                    logging.info(f"Codespace: {ctx_codespace}") 
 
            else:
                None    
            if rt_result_del_txs is not None:          
                for del_txs in rt_result_del_txs:
                    # print(del_txs.keys())
                    logging.info("----- Firing Retry Tx(s) to the chain!!! -----")
                    # logging.info(f"Delivered TXs: {del_txs}")
                    dtx_code = del_txs['code']
                    dtx_data = del_txs['data']
                    dtx_log = del_txs['log']
                    dtx_info = del_txs['info']
                    dtx_gas_wanted = del_txs['gas_wanted']
                    dtx_gas_used = del_txs['gas_used']
                    dtx_events = del_txs['events']
                    dtx_codespace = del_txs['codespace']

                    logging.info(f"Code: {dtx_code}")
                    logging.info(f"Data: {dtx_data}")
                    logging.info(f"Log: {dtx_log}")  
                    logging.info(f"Info: {dtx_info}") 
                    logging.info(f"Gas Wanted: {dtx_gas_wanted} Gas Used: {dtx_gas_used}")
                    logging.info(f"Events: {dtx_events}")
                    logging.info(f"Codespace: {dtx_codespace}") 
            else:
                None 
            logging.info(f"Process Error(s): {rt_error}")
            # logging.info(response.json())
        except httpx.ReadTimeout:
            logging.error("Read timeout while waiting for response from Skip")
            return False
        
        try:
            # Continue sending bundles if we get a Not a Skip Validator error
            if response.json()["result"]["code"] == NOT_A_SKIP_VAL_CODE:
                logging.info("Not a skip val, retyring...")
                time.sleep(DELAY_BETWEEN_SENDS)
                return None
            # If we get a 0 error code, we move on to the next transaction
            if response.json()["result"]["code"] == SUCCESS_CODE:
                self.reset = True
                logging.info("Simulation successful!")
                return True
            # If we get any other error code, we move on to the next transaction
            return False
        except KeyError:
            logging.info("KeyError in response from Skip")
            return False