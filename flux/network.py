import socket
import threading
import json as json_lib # Alias to avoid conflict
import time
import uuid
import os
import base64
import asyncio
import websockets
import logging
from typing import Dict, Callable, Optional

from .crypto import encrypt_data, decrypt_data, derive_key
from .compression import compress_file, decompress_file

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

# Constants
BUFFER_SIZE = 4096
SERVER_PORT = 5555 # Port for P2P connection. Receiver listens on this, Sender connects to this.
RELAY_URL = "ws://localhost:8765"
RELAY_TIMEOUT = 300  # 5 minutes for relay operations (e.g., waiting for receiver)

# Global state
active_transfers: Dict[str, dict] = {}
# transfer_codes dictionary is removed as the relay server now manages codes.

# Removed generate_transfer_code, register_transfer, and get_transfer_by_code
# as they are part of the old code-based P2P mechanism.

def get_local_ip() -> str:
    """Get the local IP address of the machine"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

# Add a function to get active transfer status for GUI display
def get_transfer_status(transfer_id: str = None) -> dict:
    """Get status of all transfers or a specific transfer"""
    if transfer_id:
        return active_transfers.get(transfer_id, {})
    return active_transfers

def cancel_transfer(transfer_id: str) -> bool:
    """Cancel an active transfer"""
    if transfer_id in active_transfers:
        transfer_info = active_transfers[transfer_id]
        
        current_status = transfer_info.get('status')
        if current_status in ['completed', 'failed', 'cancelled']:
            logging.info(f"Transfer {transfer_id} already in final state: {current_status}. No action for cancel.")
            return False # Already finished or cancelled

        transfer_info['status'] = 'cancelled'
        logging.info(f"Transfer {transfer_id} marked as cancelled by user.")

        # The actual closing of sockets/WebSockets will be handled by the
        # sending/receiving loops checking this 'cancelled' status.
        # For example, if a WebSocket object is stored in transfer_info:
        # if 'websocket' in transfer_info and hasattr(transfer_info['websocket'], 'close'):
        #     try:
        #         # This is tricky. If the websocket is running in an asyncio loop
        #         # managed by another thread, closing it directly from here can cause issues.
        #         # It's better if the asyncio code itself polls for the status.
        #         # asyncio.run(transfer_info['websocket'].close()) # Avoid this if possible
        #         logging.info(f"Signalled WebSocket to close for transfer {transfer_id}.")
        #     except Exception as e:
        #         logging.warning(f"Error trying to signal WebSocket close for {transfer_id}: {e}")
        
        # If a P2P socket object is stored:
        if 'p2p_socket' in transfer_info and transfer_info['p2p_socket']:
            try:
                transfer_info['p2p_socket'].close()
                logging.info(f"Closed P2P socket for cancelled transfer {transfer_id}.")
            except Exception as e:
                logging.warning(f"Error closing P2P socket for cancelled transfer {transfer_id}: {e}")
        
        return True
    return False

async def _async_send_file_via_relay(
    transfer_id: str,
    file_path: str, # Original path, for reference. compressed_file_path is used for sending.
    password: str, # Key is derived from this.
    progress_callback: Optional[Callable],
    prepared_metadata: dict, # Metadata prepared by send_file, transfer_code to be added.
    compressed_file_path: str, # Path to the (potentially) compressed file to send.
    key: bytes, # Derived encryption key.
    # salt: bytes # Salt is part of metadata, not directly needed here if key is pre-derived.
):
    """
    Manages the sender's interaction with the relay server and subsequent P2P file transfer.
    This function is intended to be run via asyncio.run() from a synchronous context.
    """
    websocket = None
    relay_transfer_code = None
    # sender_ip_from_relay = None # IP of sender as seen by relay (for info, not directly used by sender to connect)
    receiver_ip_from_relay = None # IP of receiver as seen by relay (sender connects to this)
    receiver_p2p_port_from_relay = None # P2P port of receiver from relay (sender connects to this)

    active_transfers[transfer_id]["status"] = "connecting_to_relay"
    if progress_callback:
        progress_callback(transfer_id, 0, "Connecting to relay server...")
    
    try:
        async with websockets.connect(RELAY_URL, open_timeout=10, close_timeout=10) as ws:
            websocket = ws
            # Store websocket in active_transfers for potential cancellation/cleanup needs,
            # though direct async operations from synchronous cancel_transfer are complex.
            # The primary cancellation mechanism is the status flag.
            active_transfers[transfer_id]['websocket_obj'] = websocket 

            # 1. Register Sender
            logging.info(f"Transfer {transfer_id}: Registering with relay. Sending port {SERVER_PORT} for P2P.")
            await websocket.send(json_lib.dumps({
                "type": "register_sender",
                "port": SERVER_PORT # The port this sender *would* listen on if roles were reversed,
                                   # or the port receiver should use if relay tells receiver to connect to sender.
                                   # For FLUX (sender connects out), this is more for relay's record / to pass to receiver.
            }))
            
            # Wait for registration confirmation
            response_str = await asyncio.wait_for(websocket.recv(), timeout=30)
            response = json_lib.loads(response_str)

            if response.get("type") == "sender_registered":
                relay_transfer_code = response.get("code")
                # sender_ip_from_relay = response.get("ip") # Sender's public IP as seen by relay
                if not relay_transfer_code:
                    raise Exception("Relay did not provide a transfer code.")
                
                active_transfers[transfer_id]["transfer_code"] = relay_transfer_code
                # active_transfers[transfer_id]["sender_ip_from_relay"] = sender_ip_from_relay
                active_transfers[transfer_id]["status"] = "waiting_for_receiver"
                logging.info(f"Transfer {transfer_id}: Registered with relay. Code: {relay_transfer_code}")
                if progress_callback:
                    progress_callback(transfer_id, 0, f"Registered. Transfer Code: {relay_transfer_code}")
            else:
                error_msg = response.get("message", "Relay registration failed.")
                raise Exception(f"Relay Error (registration): {error_msg}")

            # 2. Wait for Receiver
            logging.info(f"Transfer {transfer_id}: Waiting for receiver...")
            
            receiver_found_event = asyncio.Event()
            listener_task_exception = None

            async def listen_for_receiver_messages():
                nonlocal receiver_ip_from_relay, receiver_p2p_port_from_relay, listener_task_exception
                try:
                    while not receiver_found_event.is_set():
                        if active_transfers[transfer_id].get('status') == 'cancelled':
                            logging.info(f"Transfer {transfer_id}: Cancelled by user while waiting for receiver.")
                            # receiver_found_event.set() # Signal to stop waiting
                            raise asyncio.CancelledError("Transfer cancelled by user.")

                        message_str = await asyncio.wait_for(websocket.recv(), timeout=5) # Periodically check cancel status
                        message = json_lib.loads(message_str)
                        msg_type = message.get("type")

                        if msg_type == "receiver_found":
                            receiver_ip_from_relay = message.get("receiver_ip")
                            receiver_p2p_port_from_relay = message.get("receiver_port")
                            if receiver_ip_from_relay and isinstance(receiver_p2p_port_from_relay, int):
                                logging.info(f"Transfer {transfer_id}: Receiver found by relay. IP: {receiver_ip_from_relay}, Port: {receiver_p2p_port_from_relay}")
                                receiver_found_event.set()
                            else:
                                logging.error(f"Transfer {transfer_id}: Invalid 'receiver_found' message from relay: {message}")
                                # Keep waiting or raise error? For now, keep waiting for a valid one or timeout.
                        elif msg_type == "info" and message.get("message") == "Receiver disconnected.":
                            logging.info(f"Transfer {transfer_id}: Relay informed that receiver disconnected. Waiting again or timing out.")
                            # No specific action here, main timeout will handle it or a new receiver_found might come.
                        elif msg_type == "error":
                            logging.error(f"Transfer {transfer_id}: Received error from relay: {message.get('message')}")
                            # Depending on error, might want to stop. For now, log and let timeout occur.
                        else:
                            logging.warning(f"Transfer {transfer_id}: Ignoring unexpected message from relay: {msg_type}")
                except asyncio.TimeoutError: # Timeout from wait_for(websocket.recv(), timeout=5)
                    pass # This is expected, to allow checking the cancellation status
                except websockets.exceptions.ConnectionClosed:
                    logging.warning(f"Transfer {transfer_id}: Relay connection closed unexpectedly while waiting for receiver.")
                    if not receiver_found_event.is_set(): # If not already found, this is an error
                        listener_task_exception = Exception("Relay connection closed prematurely.")
                        receiver_found_event.set() # Stop waiting
                except asyncio.CancelledError: # Explicitly cancelled
                     listener_task_exception = asyncio.CancelledError("Transfer cancelled by user during relay wait.")
                     receiver_found_event.set() # Stop waiting
                except Exception as e:
                    logging.error(f"Transfer {transfer_id}: Error in relay listener task: {e}", exc_info=True)
                    if not receiver_found_event.is_set():
                        listener_task_exception = e
                        receiver_found_event.set() # Stop waiting
            
            listener_task = asyncio.create_task(listen_for_receiver_messages())
            
            try:
                # Wait for the receiver_found_event to be set, with the overall RELAY_TIMEOUT
                await asyncio.wait_for(receiver_found_event.wait(), timeout=RELAY_TIMEOUT)
            except asyncio.TimeoutError:
                listener_task.cancel() # Cancel the listener task
                await asyncio.gather(listener_task, return_exceptions=True) # Ensure it's cleaned up
                raise Exception(f"Timeout ({RELAY_TIMEOUT}s) waiting for receiver on relay.")
            
            await listener_task # Ensure listener task completes and propagate its errors if any

            if listener_task_exception:
                if isinstance(listener_task_exception, asyncio.CancelledError):
                     raise Exception("Transfer cancelled by user.") # Convert to regular Exception for sync context
                raise listener_task_exception # Re-raise other exceptions from listener

            if not (receiver_ip_from_relay and isinstance(receiver_p2p_port_from_relay, int)):
                raise Exception("Failed to get valid receiver details from relay.")

            active_transfers[transfer_id]["receiver_ip_from_relay"] = receiver_ip_from_relay
            active_transfers[transfer_id]["receiver_p2p_port_from_relay"] = receiver_p2p_port_from_relay
            active_transfers[transfer_id]["status"] = "receiver_found"
            logging.info(f"Transfer {transfer_id}: Receiver details obtained. IP: {receiver_ip_from_relay}, Port: {receiver_p2p_port_from_relay}")
            if progress_callback:
                progress_callback(transfer_id, 0, f"Receiver found. Connecting for P2P transfer...")
        
        # WebSocket connection is closed automatically by 'async with'
        logging.info(f"Transfer {transfer_id}: Closed WebSocket connection to relay.")
        active_transfers[transfer_id].pop('websocket_obj', None) # Clean up reference

        # 3. P2P Transfer (adapted existing logic)
        active_transfers[transfer_id]["status"] = "connecting_to_peer"
        if progress_callback:
            progress_callback(transfer_id, 0, f"Connecting to peer at {receiver_ip_from_relay}:{receiver_p2p_port_from_relay}...")

        # Update metadata with the transfer code from relay for the receiver to see (if needed by receiver)
        final_metadata = prepared_metadata.copy()
        final_metadata["transfer_code"] = relay_transfer_code 
        
        p2p_socket = None
        try:
            p2p_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            active_transfers[transfer_id]['p2p_socket'] = p2p_socket # For potential cancellation
            p2p_socket.settimeout(30) # Connection timeout for P2P
            p2p_socket.connect((receiver_ip_from_relay, receiver_p2p_port_from_relay))
            p2p_socket.settimeout(None) # Reset timeout for data transfer phase

            logging.info(f"Transfer {transfer_id}: P2P connection established with {receiver_ip_from_relay}:{receiver_p2p_port_from_relay}.")
            active_transfers[transfer_id]["status"] = "sending_p2p"
            if progress_callback:
                progress_callback(transfer_id, 0, "P2P connected. Sending file data...")

            # Send metadata
            p2p_socket.sendall(json_lib.dumps(final_metadata).encode() + b"\n")
            
            bytes_sent = 0
            file_size_to_send = final_metadata["compressed_size"] # Size of the file being sent (potentially compressed)
            
            with open(compressed_file_path, 'rb') as f:
                while True:
                    if active_transfers[transfer_id].get('status') == 'cancelled':
                        logging.info(f"Transfer {transfer_id}: Cancelled by user during P2P send.")
                        raise Exception("Transfer cancelled by user during P2P send.")
                    
                    chunk = f.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    
                    encrypted_chunk = encrypt_data(chunk, key)
                    p2p_socket.sendall(encrypted_chunk)
                    
                    bytes_sent += len(chunk) # Progress based on original chunk size before encryption
                    progress = int((bytes_sent / file_size_to_send) * 100)
                    
                    active_transfers[transfer_id]["progress"] = progress
                    if progress_callback:
                        # Avoid too frequent UI updates if progress callback is expensive
                        if progress % 5 == 0 or progress == 100: 
                            progress_callback(transfer_id, progress, f"Sending: {progress}%")
            
            logging.info(f"Transfer {transfer_id}: File data sent completely.")
            active_transfers[transfer_id]["status"] = "completed"
            active_transfers[transfer_id]["progress"] = 100
            if progress_callback:
                progress_callback(transfer_id, 100, "Transfer completed successfully.")

        except socket.error as se:
            logging.error(f"Transfer {transfer_id}: P2P socket error: {se}", exc_info=True)
            raise Exception(f"P2P Connection/Transfer Error: {se}") # Re-raise for outer handler
        except Exception as e: # Catch other P2P errors
            logging.error(f"Transfer {transfer_id}: Error during P2P transfer: {e}", exc_info=True)
            raise # Re-raise for outer handler
        finally:
            if p2p_socket:
                p2p_socket.close()
            active_transfers[transfer_id].pop('p2p_socket', None)
            # Clean up compressed file only if P2P transfer was attempted (success or fail)
            if os.path.exists(compressed_file_path):
                 os.remove(compressed_file_path)
                 logging.info(f"Transfer {transfer_id}: Removed temporary compressed file {compressed_file_path}.")

    except websockets.exceptions.ConnectionClosedError as e:
        logging.error(f"Transfer {transfer_id}: Relay connection closed unexpectedly: {e}", exc_info=True)
        active_transfers[transfer_id]["status"] = "failed"
        active_transfers[transfer_id]["error_message"] = f"Relay connection closed: {e.args[0]}"
        if progress_callback:
            progress_callback(transfer_id, active_transfers[transfer_id].get("progress", 0), f"Error: Relay connection closed.")
    except asyncio.TimeoutError: # Covers wait_for timeouts for relay operations
        logging.warning(f"Transfer {transfer_id}: Operation timed out with relay.", exc_info=True)
        active_transfers[transfer_id]["status"] = "failed"
        active_transfers[transfer_id]["error_message"] = "Timeout with relay server."
        if progress_callback:
            progress_callback(transfer_id, active_transfers[transfer_id].get("progress", 0), "Error: Timeout with relay.")
    except Exception as e:
        logging.error(f"Transfer {transfer_id}: An error occurred: {e}", exc_info=True)
        # Avoid overwriting specific P2P cancellation message if that was the cause
        if not (isinstance(e, Exception) and str(e) == "Transfer cancelled by user during P2P send."):
            if not (isinstance(e, Exception) and str(e) == "Transfer cancelled by user."): # from relay wait
                 active_transfers[transfer_id]["status"] = "failed"
                 active_transfers[transfer_id]["error_message"] = str(e)
        if progress_callback:
            progress_callback(transfer_id, active_transfers[transfer_id].get("progress", 0), f"Error: {str(e)}")
    finally:
        if websocket and websocket.open: # Ensure websocket is closed if an error occurred before 'async with' exited
            try:
                await websocket.close()
                logging.info(f"Transfer {transfer_id}: Ensured WebSocket is closed in final error handler.")
            except Exception as e_ws_close:
                logging.error(f"Transfer {transfer_id}: Error closing WebSocket in finally block: {e_ws_close}")
        
        active_transfers[transfer_id].pop('websocket_obj', None) # Ensure cleanup from active_transfers

        # If the process failed before P2P part started or P2P failed very early, compressed file might still exist.
        if active_transfers[transfer_id]["status"] == "failed" and os.path.exists(compressed_file_path):
            # Check if error message indicates P2P was even attempted. If not, safe to remove.
            # This check is a bit heuristic. The P2P block has its own finally for removal.
            if "P2P" not in active_transfers[transfer_id].get("error_message", ""):
                try:
                    os.remove(compressed_file_path)
                    logging.info(f"Transfer {transfer_id}: Cleaned up compressed file due to pre-P2P failure: {compressed_file_path}")
                except OSError as e_os:
                    logging.warning(f"Transfer {transfer_id}: Error removing compressed file in final cleanup: {e_os}")

def send_file(file_path: str, password: str, progress_callback: Optional[Callable] = None) -> str:
    """
    Sends a file using the relay server for signaling and then establishes a P2P connection.
    Returns a transfer_id for tracking in the GUI.
    """
    transfer_id = str(uuid.uuid4())
    file_name = os.path.basename(file_path)
    
    if not os.path.exists(file_path):
        logging.error(f"File not found: {file_path} for transfer {transfer_id}")
        # This exception should ideally be caught by the calling UI code
        # and displayed to the user gracefully.
        raise FileNotFoundError(f"File {file_path} not found.")

    original_file_size = os.path.getsize(file_path)

    active_transfers[transfer_id] = {
        "file_name": file_name,
        "original_size": original_file_size,
        "status": "initiating", # Initial status
        "progress": 0,
        "start_time": time.time(),
        "transfer_code": None, # Will be updated by relay
        "error_message": None
    }
    if progress_callback:
        progress_callback(transfer_id, 0, "Initiating transfer...")

    compressed_file_path = "" # Initialize, will be set after compression
    try:
        # Compress the file
        active_transfers[transfer_id]["status"] = "compressing"
        if progress_callback:
            progress_callback(transfer_id, 0, "Compressing file...")
        
        compressed_file_path = compress_file(file_path)
        compressed_size = os.path.getsize(compressed_file_path)
        logging.info(f"Transfer {transfer_id}: File compressed from {original_file_size} to {compressed_size} bytes.")

    except Exception as e:
        logging.error(f"Transfer {transfer_id}: Compression failed: {e}", exc_info=True)
        active_transfers[transfer_id]["status"] = "failed"
        active_transfers[transfer_id]["error_message"] = f"Compression failed: {str(e)}"
        if progress_callback:
            progress_callback(transfer_id, 0, f"Error: Compression failed - {str(e)}")
        # No need to remove compressed_file_path here as it might not exist or be partial
        return transfer_id

    # Generate salt and derive key
    salt = os.urandom(16)
    key = derive_key(password, salt)
    
    # Prepare metadata (transfer_code will be added later by _async_send_file_via_relay)
    is_compressed = compressed_file_path != file_path # True if compression actually reduced size / changed path
    prepared_metadata = {
        "transfer_id": transfer_id, # For receiver to cross-check if needed, though relay code is primary
        "file_name": file_name,
        "original_size": original_file_size,
        "compressed_size": compressed_size, # This is the size of the data to be transferred P2P
        "salt": base64.b64encode(salt).decode(),
        "is_compressed": is_compressed
        # "transfer_code" is omitted here; will be added by _async_send_file_via_relay from relay response
    }
    
    # The actual work (relay + P2P) is done in this thread by running the async helper.
    def transfer_operation_thread():
        try:
            # Ensure a new event loop for this thread if not running on main thread's loop
            # asyncio.set_event_loop(asyncio.new_event_loop()) # Usually not needed if just using asyncio.run()
            asyncio.run(_async_send_file_via_relay(
                transfer_id=transfer_id,
                file_path=file_path, 
                password=password, # Passed for consistency, though key is used directly
                progress_callback=progress_callback,
                prepared_metadata=prepared_metadata,
                compressed_file_path=compressed_file_path,
                key=key
            ))
        except Exception as e:
            # This top-level catch is for unexpected errors from asyncio.run() itself or if
            # _async_send_file_via_relay raised an error that wasn't caught by its internal handlers
            # (which should be rare as it has its own comprehensive try-except-finally).
            logging.critical(f"Transfer {transfer_id}: Critical error in async operation thread: {e}", exc_info=True)
            if active_transfers.get(transfer_id) and active_transfers[transfer_id]['status'] not in ['completed', 'failed', 'cancelled']:
                active_transfers[transfer_id]["status"] = "failed"
                active_transfers[transfer_id]["error_message"] = f"Critical error: {str(e)}"
                if progress_callback:
                    progress_callback(transfer_id, active_transfers[transfer_id].get("progress", 0), f"Critical Error: {str(e)}")
            
            # Ensure cleanup of compressed file if it exists and transfer failed badly
            if os.path.exists(compressed_file_path):
                try:
                    os.remove(compressed_file_path)
                    logging.info(f"Transfer {transfer_id}: Cleaned up compressed file due to critical error in thread.")
                except OSError as e_os:
                    logging.warning(f"Transfer {transfer_id}: Error removing compressed file in critical error handler: {e_os}")

    # Start the transfer operation in a new thread
    # This is consistent with how flux/core.py expects send_file to behave (not block main UI thread)
    thread = threading.Thread(target=transfer_operation_thread)
    thread.daemon = True # Ensures thread exits when main program exits
    thread.start()
    
    return transfer_id

class ReceiverServer:
    """
    Manages the receiver-side operations, including relay communication and P2P file reception.
    """
    def __init__(self, save_dir: str, password: str, user_entered_code: str, progress_callback: Optional[Callable] = None):
        self.save_dir = save_dir
        self.password = password
        self.user_entered_code = user_entered_code # This is the transfer code input by the user
        self.transfer_id = self.user_entered_code # Use the transfer code as the key for active_transfers on receiver side
        self.progress_callback = progress_callback
        
        self._stop_event = threading.Event() # Used to signal cancellation to the running thread
        self._thread = None # Thread that will run the async receiving process
        self.p2p_server_socket = None # Store the P2P listening server socket for potential cleanup

        # Initialize status in active_transfers
        # This allows the UI to immediately track this pending transfer
        active_transfers[self.transfer_id] = {
            "file_name": f"Incoming (Code: {self.user_entered_code})",
            "status": "receiver_initialized", # Initial status
            "progress": 0,
            "start_time": time.time(),
            "transfer_code": self.user_entered_code,
            "error_message": None,
            "is_receiver": True # Differentiate from sender transfers in UI if needed
        }
        if self.progress_callback:
            self.progress_callback(self.transfer_id, 0, f"Receiver ready for code {self.user_entered_code}. Waiting to start.")
        logging.info(f"ReceiverServer initialized for transfer_id (code): {self.transfer_id}")

    def start_async_receive(self):
        """
        Starts the asynchronous file receiving process (relay + P2P) in a new thread.
        """
        if self._thread and self._thread.is_alive():
            logging.warning(f"Transfer {self.transfer_id}: Receive process already running.")
            return

        active_transfers[self.transfer_id]["status"] = "receiver_starting"
        if self.progress_callback:
            self.progress_callback(self.transfer_id, 0, "Starting receiver...")

        self._thread = threading.Thread(target=self._run_receive_logic)
        self._thread.daemon = True
        self._thread.start()
        logging.info(f"Transfer {self.transfer_id}: Receiver thread started.")

    def _run_receive_logic(self):
        """
        Wrapper to run the _async_receive_file_via_relay function.
        """
        try:
            asyncio.run(_async_receive_file_via_relay(
                transfer_id=self.transfer_id, # Which is the user_entered_code
                save_dir=self.save_dir,
                password=self.password,
                progress_callback=self.progress_callback,
                receiver_server_instance=self # Pass instance for socket storage
            ))
        except Exception as e:
            logging.critical(f"Transfer {self.transfer_id}: Critical error in receiver async operation thread: {e}", exc_info=True)
            if self.transfer_id in active_transfers and active_transfers[self.transfer_id]['status'] not in ['completed', 'failed', 'cancelled']:
                active_transfers[self.transfer_id]["status"] = "failed"
                active_transfers[self.transfer_id]["error_message"] = f"Critical receiver error: {str(e)}"
                if self.progress_callback:
                    self.progress_callback(self.transfer_id, active_transfers[self.transfer_id].get("progress", 0), f"Critical Receiver Error: {str(e)}")

    def close(self):
        """
        Stops the receiver operation (signals cancellation) and attempts to clean up resources.
        """
        logging.info(f"Transfer {self.transfer_id}: Close called for receiver.")
        self._stop_event.set() # Signal the async logic to stop, if it's polling this event

        # Call cancel_transfer to update status in active_transfers.
        # cancel_transfer itself doesn't do much async resource cleanup directly.
        # The async loop (_async_receive_file_via_relay) should check active_transfers status.
        cancel_transfer(self.transfer_id) # This will set status to 'cancelled'

        # Attempt to close the P2P server socket if it's open
        if self.p2p_server_socket:
            try:
                self.p2p_server_socket.close()
                logging.info(f"Transfer {self.transfer_id}: P2P server socket closed via ReceiverServer.close().")
            except Exception as e:
                logging.warning(f"Transfer {self.transfer_id}: Error closing P2P server socket in ReceiverServer.close(): {e}")
        
        if self._thread and self._thread.is_alive():
            logging.info(f"Transfer {self.transfer_id}: Receiver thread is alive, joining with timeout.")
            self._thread.join(timeout=5) # Wait briefly for the thread to exit
            if self.progress_callback and self._thread.is_alive():
                 logging.warning(f"Transfer {self.transfer_id}: Receiver thread did not exit cleanly after close signal.")
        
        logging.info(f"Transfer {self.transfer_id}: Receiver close process completed.")

async def _async_receive_file_via_relay(
    transfer_id: str, # This is the user_entered_code
    save_dir: str,
    password: str,
    progress_callback: Optional[Callable],
    receiver_server_instance: ReceiverServer # To store p2p_server_socket for cancellation
):
    """
    Manages the receiver's interaction with the relay server and subsequent P2P file reception.
    """
    websocket = None
    sender_ip_from_relay = None
    sender_p2p_port_from_relay = None
    # receiver_ip_as_seen_by_relay = None # For info, not directly used by receiver to connect

    active_transfers[transfer_id]["status"] = "connecting_to_relay"
    if progress_callback:
        progress_callback(transfer_id, 0, f"Connecting to relay with code {transfer_id}...")

    try:
        # 1. Connect to Relay and get Sender's P2P details
        async with websockets.connect(RELAY_URL, open_timeout=10, close_timeout=10) as ws:
            websocket = ws
            active_transfers[transfer_id]['websocket_obj'] = websocket # For potential debug/future use

            logging.info(f"Transfer {transfer_id}: Connecting to relay as receiver. Code: {transfer_id}, P2P Port: {SERVER_PORT}")
            await websocket.send(json_lib.dumps({
                "type": "connect_receiver",
                "code": transfer_id, # User-entered code
                "port": SERVER_PORT # The port this receiver will listen on for P2P
            }))

            response_str = await asyncio.wait_for(websocket.recv(), timeout=RELAY_TIMEOUT) # Timeout for relay response
            response = json_lib.loads(response_str)

            if response.get("type") == "receiver_connected":
                sender_ip_from_relay = response.get("sender_ip")
                sender_p2p_port_from_relay = response.get("sender_port")
                # receiver_ip_as_seen_by_relay = response.get("receiver_ip") # Our IP as seen by relay

                if not (sender_ip_from_relay and isinstance(sender_p2p_port_from_relay, int)):
                    raise Exception("Relay did not provide valid sender IP or port.")
                
                active_transfers[transfer_id]["sender_ip_from_relay"] = sender_ip_from_relay
                active_transfers[transfer_id]["sender_p2p_port_from_relay"] = sender_p2p_port_from_relay
                active_transfers[transfer_id]["status"] = "waiting_for_sender_p2p"
                logging.info(f"Transfer {transfer_id}: Connected to relay. Sender expected from {sender_ip_from_relay}:{sender_p2p_port_from_relay}. My P2P listening port: {SERVER_PORT}")
                if progress_callback:
                    progress_callback(transfer_id, 0, f"Waiting for sender. Listening on port {SERVER_PORT}.")
            elif response.get("type") == "error":
                error_msg = response.get("message", "Relay connection failed for receiver.")
                raise Exception(f"Relay Error: {error_msg}")
            else:
                raise Exception(f"Unexpected response from relay: {response.get('type')}")
        
        # WebSocket closed after 'async with'
        active_transfers[transfer_id].pop('websocket_obj', None)
        logging.info(f"Transfer {transfer_id}: Disconnected from relay. Proceeding to P2P listen.")

        # 2. P2P Server Setup and File Reception (adapted from old receive_file)
        p2p_server_socket = None
        client_socket = None
        temp_compressed_path = None # Ensure it's defined for finally block

        try:
            p2p_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            p2p_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Store the server socket on the instance so it can be closed by ReceiverServer.close()
            receiver_server_instance.p2p_server_socket = p2p_server_socket

            # Bind and Listen
            # TODO: Add retry logic for bind if necessary, like in old receiver_thread
            p2p_server_socket.bind(('0.0.0.0', SERVER_PORT))
            p2p_server_socket.listen(1)
            logging.info(f"Transfer {transfer_id}: P2P server listening on 0.0.0.0:{SERVER_PORT}")
            
            # Set a timeout for accept, in case sender never connects after relay phase
            p2p_server_socket.settimeout(120) # 2 minutes for sender to initiate P2P connection

            if active_transfers[transfer_id].get('status') == 'cancelled':
                raise Exception("Transfer cancelled by user before P2P connection.")

            try:
                client_socket, client_address = p2p_server_socket.accept()
            except socket.timeout:
                logging.warning(f"Transfer {transfer_id}: Timeout waiting for sender P2P connection on port {SERVER_PORT}.")
                raise Exception(f"Timeout waiting for sender to connect P2P.")
            
            receiver_server_instance.p2p_server_socket = None # Socket now in use, can't be closed by cancel directly
                                                              # client_socket should be closed instead.
                                                              # For simplicity, we let the flow handle client_socket.

            logging.info(f"Transfer {transfer_id}: P2P connection accepted from {client_address}. Sender IP from relay was {sender_ip_from_relay}.")
            # Optionally, validate client_address[0] against sender_ip_from_relay if strict matching is needed.

            active_transfers[transfer_id]["status"] = "receiving_metadata"
            if progress_callback:
                progress_callback(transfer_id, 0, "Sender connected. Receiving file details...")
            
            client_socket.settimeout(30) # Timeout for receiving metadata

            metadata_bytes = b""
            while b"\n" not in metadata_bytes:
                if active_transfers[transfer_id].get('status') == 'cancelled':
                    raise Exception("Transfer cancelled while receiving metadata.")
                chunk = client_socket.recv(BUFFER_SIZE)
                if not chunk: raise Exception("Connection closed by sender during metadata.")
                metadata_bytes += chunk
            
            metadata_str, remaining_data = metadata_bytes.split(b"\n", 1)
            metadata = json_lib.loads(metadata_str.decode())
            
            file_name = metadata.get("file_name", "unknown_file")
            original_size = metadata.get("original_size")
            compressed_size = metadata.get("compressed_size")
            salt_b64 = metadata.get("salt")
            is_compressed = metadata.get("is_compressed", True) # Default to true if missing
            # sender_transfer_id = metadata.get("transfer_id") # This is sender's UUID, for logging/debug
            # received_transfer_code = metadata.get("transfer_code") # This should match our transfer_id (user_entered_code)

            if not all([file_name, isinstance(compressed_size, int), salt_b64]):
                raise Exception("Incomplete metadata received from sender.")

            active_transfers[transfer_id]["file_name"] = file_name # Update with actual filename
            active_transfers[transfer_id]["original_size"] = original_size
            logging.info(f"Transfer {transfer_id}: Received metadata: {metadata}")

            key = derive_key(password, base64.b64decode(salt_b64))

            # Ensure save_dir exists
            if not os.path.exists(save_dir):
                os.makedirs(save_dir, exist_ok=True)
            
            # Paths for saving
            temp_compressed_path = os.path.join(save_dir, f"{file_name}.flux_temp") # Temp name for received (possibly compressed) data
            final_output_path = os.path.join(save_dir, file_name) # Final path after potential decompression

            active_transfers[transfer_id]["status"] = "receiving_data"
            if progress_callback:
                progress_callback(transfer_id, 0, "Receiving file data...")

            bytes_received = 0
            with open(temp_compressed_path, 'wb') as f:
                if remaining_data: # Handle data received along with metadata
                    decrypted_chunk = decrypt_data(remaining_data, key)
                    f.write(decrypted_chunk)
                    bytes_received += len(decrypted_chunk) # Count decrypted length towards compressed_size

                client_socket.settimeout(60) # Longer timeout for file data transfer
                while bytes_received < compressed_size:
                    if active_transfers[transfer_id].get('status') == 'cancelled':
                        raise Exception("Transfer cancelled while receiving file data.")
                    
                    chunk = client_socket.recv(BUFFER_SIZE)
                    if not chunk:
                        if bytes_received < compressed_size:
                            raise Exception("Connection closed by sender prematurely during data transfer.")
                        break # Expected EOF
                    
                    decrypted_chunk = decrypt_data(chunk, key)
                    f.write(decrypted_chunk)
                    bytes_received += len(decrypted_chunk)
                    
                    progress = int((bytes_received / compressed_size) * 100)
                    active_transfers[transfer_id]["progress"] = progress
                    if progress_callback and (progress % 5 == 0 or progress == 100):
                        progress_callback(transfer_id, progress, f"Receiving: {progress}%")
            
            logging.info(f"Transfer {transfer_id}: All encrypted data received ({bytes_received} bytes).")

            # Decompression (if applicable)
            if is_compressed:
                active_transfers[transfer_id]["status"] = "decompressing"
                if progress_callback: progress_callback(transfer_id, 100, "Decompressing file...")
                logging.info(f"Transfer {transfer_id}: Decompressing {temp_compressed_path} to {final_output_path}")
                
                # Decompress to a temporary output first to avoid data loss on error
                decomp_temp_output_path = final_output_path + ".decomp_tmp"
                decompress_file(temp_compressed_path, decomp_temp_output_path)
                os.replace(decomp_temp_output_path, final_output_path) # Atomic move if possible
                logging.info(f"Transfer {transfer_id}: Decompression successful.")
            else:
                os.replace(temp_compressed_path, final_output_path)
                logging.info(f"Transfer {transfer_id}: File was not compressed, moved directly.")
            
            temp_compressed_path = None # Signal that original temp file is handled (moved or deleted after decompress)

            active_transfers[transfer_id]["status"] = "completed"
            active_transfers[transfer_id]["progress"] = 100
            active_transfers[transfer_id]["file_path"] = final_output_path # Store final path
            if progress_callback:
                progress_callback(transfer_id, 100, "File received successfully!")
            logging.info(f"Transfer {transfer_id}: Completed successfully. File saved to {final_output_path}")

        except socket.error as se:
            logging.error(f"Transfer {transfer_id}: P2P socket error during receive: {se}", exc_info=True)
            active_transfers[transfer_id]["error_message"] = f"P2P Network Error: {se}"
            raise # Re-raise for outer handler to set status to failed
        except Exception as e: # Catch other P2P errors
            logging.error(f"Transfer {transfer_id}: Error during P2P receive phase: {e}", exc_info=True)
            if "Transfer cancelled" not in str(e): # Avoid overwriting specific cancellation message
                 active_transfers[transfer_id]["error_message"] = str(e)
            raise # Re-raise
        finally:
            if client_socket: client_socket.close()
            if p2p_server_socket: p2p_server_socket.close()
            receiver_server_instance.p2p_server_socket = None # Clear reference
            if temp_compressed_path and os.path.exists(temp_compressed_path):
                try:
                    os.remove(temp_compressed_path)
                    logging.info(f"Transfer {transfer_id}: Cleaned up temporary file {temp_compressed_path}.")
                except OSError:
                    logging.warning(f"Transfer {transfer_id}: Error removing temp file {temp_compressed_path}.")

    except websockets.exceptions.ConnectionClosedError as e:
        logging.error(f"Transfer {transfer_id}: Relay connection closed unexpectedly: {e}", exc_info=True)
        active_transfers[transfer_id]["status"] = "failed"
        active_transfers[transfer_id]["error_message"] = f"Relay connection closed: {e.args[0]}"
    except asyncio.TimeoutError:
        logging.warning(f"Transfer {transfer_id}: Operation timed out with relay.", exc_info=True)
        active_transfers[transfer_id]["status"] = "failed"
        active_transfers[transfer_id]["error_message"] = "Timeout connecting to relay."
    except Exception as e:
        logging.error(f"Transfer {transfer_id}: An error occurred in receiver logic: {e}", exc_info=True)
        if active_transfers[transfer_id]["status"] not in ["completed", "failed", "cancelled"]:
            active_transfers[transfer_id]["status"] = "failed"
        if "error_message" not in active_transfers[transfer_id] or not active_transfers[transfer_id]["error_message"]:
             active_transfers[transfer_id]["error_message"] = str(e)
    finally:
        if websocket and websocket.open:
            await websocket.close()
        active_transfers[transfer_id].pop('websocket_obj', None)
        
        current_status = active_transfers[transfer_id].get("status")
        if current_status not in ["completed", "cancelled"] : # If not completed or cancelled, mark as failed.
             active_transfers[transfer_id]["status"] = "failed"

        if progress_callback: # Final status update to UI
            progress_callback(transfer_id, active_transfers[transfer_id].get("progress",0), 
                              f"Status: {active_transfers[transfer_id]['status']}. " + \
                              (active_transfers[transfer_id].get('error_message', "") if active_transfers[transfer_id]['status'] == 'failed' else ""))
        logging.info(f"Transfer {transfer_id}: Async receive logic finished with status: {active_transfers[transfer_id]['status']}.")


def start_receiver_server(save_dir: str, password: str, transfer_code: str, progress_callback: Optional[Callable] = None) -> ReceiverServer:
    """
    Initializes the ReceiverServer instance and starts the receiving process.
    The receiving process (relay communication and P2P) runs in a separate thread managed by ReceiverServer.
    """
    if not transfer_code or not isinstance(transfer_code, str) or not transfer_code.strip():
        # Handle invalid transfer_code case gracefully.
        # This might involve raising an error or returning a specific status.
        # For now, log and let it potentially fail in ReceiverServer init or later.
        logging.error("start_receiver_server called with invalid transfer_code.")
        # Depending on strictness, could raise ValueError("Transfer code cannot be empty.")
        # or return a dummy/error state ReceiverServer object.
        # For now, we proceed, and ReceiverServer __init__ will use it as transfer_id.

    logging.info(f"Creating and starting ReceiverServer for code: {transfer_code}")
    
    # ReceiverServer __init__ now uses user_entered_code as transfer_id and initializes active_transfers
    server = ReceiverServer(save_dir=save_dir, 
                            password=password, 
                            user_entered_code=transfer_code, 
                            progress_callback=progress_callback)
    
    # Start the asynchronous receiving process (which runs in a new thread)
    server.start_async_receive()
    
    return server

def receive_file(save_dir: str, password: str, transfer_code: str, 
                progress_callback: Optional[Callable] = None) -> Optional[str]:
    """
    DEPRECATED: This function is deprecated. Use `start_receiver_server` instead,
    which returns a ReceiverServer instance that manages the transfer.

    This function previously attempted to handle direct P2P receiving based on a code.
    The new relay-based system uses `start_receiver_server` to initiate
    a `ReceiverServer` object, which then handles the asynchronous receiving process.
    """
    logging.warning(
        "DEPRECATED: receive_file() was called. "
        "This method is outdated for relay-based transfers. "
        f"Use start_receiver_server() and ReceiverServer.start_async_receive() for code {transfer_code}."
    )
    
    # To maintain some backward compatibility for any direct calls expecting a transfer_id string,
    # and to allow minimal UI feedback if it's somehow still called:
    # We can create a ReceiverServer instance and immediately mark it as deprecated/failed.
    
    if not transfer_code or not isinstance(transfer_code, str) or not transfer_code.strip():
        logging.error("receive_file (deprecated) called with invalid transfer_code.")
        if progress_callback:
            # Use a placeholder ID for the callback if transfer_code is unusable
            err_transfer_id = str(uuid.uuid4())
            progress_callback(err_transfer_id, 0, "Error: Invalid transfer code provided for deprecated receiver.")
        return None # Or raise ValueError

    # Use the transfer_code as the transfer_id for consistency with new receiver logic
    # This will also be the key in active_transfers.
    current_transfer_id = transfer_code 

    message = "Deprecated receiver function called. Please update to use the new receiver start mechanism."
    
    active_transfers[current_transfer_id] = {
        "file_name": f"Deprecated call for {transfer_code}",
        "status": "failed", # Mark as failed immediately
        "progress": 0,
        "start_time": time.time(),
        "transfer_code": transfer_code,
        "error_message": message,
        "is_receiver": True
    }
    
    if progress_callback:
        progress_callback(current_transfer_id, 0, message)
        
    # No actual receiving logic is started here.
    # Return the transfer_code (used as transfer_id) for any old systems that might expect it.
    return current_transfer_id