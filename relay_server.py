import asyncio
import websockets
import json
import random
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

active_senders = {}
PORT = 8765
HOST = "0.0.0.0"
CODE_EXPIRY_DURATION = 600  # 10 minutes

def generate_unique_code():
    """Generates a unique 6-digit numerical code for transfers."""
    while True:
        code = "".join(random.choices("0123456789", k=6))
        if code not in active_senders:
            return code

async def handle_client(websocket, path):
    client_ip, client_port = websocket.remote_address
    logging.info(f"Client connected from {client_ip}:{client_port}")
    registered_code = None # To track if this client registered as a sender

    try:
        async for message_str in websocket:
            try:
                message = json.loads(message_str)
                msg_type = message.get("type")

                if msg_type == "register_sender":
                    sender_p2p_port = message.get("port")
                    if not isinstance(sender_p2p_port, int):
                        await websocket.send(json.dumps({"type": "error", "message": "Invalid port provided."}))
                        continue

                    code = generate_unique_code()
                    active_senders[code] = {
                        "sender_ws": websocket,
                        "sender_ip": client_ip,
                        "sender_port": sender_p2p_port,
                        "receiver_ws": None,
                        "timestamp": time.time()
                    }
                    registered_code = code # Mark this connection as a registered sender
                    await websocket.send(json.dumps({"type": "sender_registered", "code": code, "ip": client_ip}))
                    logging.info(f"Sender registered with code {code} from {client_ip}:{sender_p2p_port}")

                elif msg_type == "connect_receiver":
                    code = message.get("code")
                    receiver_p2p_port = message.get("port")

                    if not isinstance(receiver_p2p_port, int):
                        await websocket.send(json.dumps({"type": "error", "message": "Invalid port provided."}))
                        continue

                    if code in active_senders and active_senders[code]["receiver_ws"] is None:
                        sender_data = active_senders[code]
                        sender_data["receiver_ws"] = websocket
                        # No need to store receiver_port in active_senders, it's sent directly

                        # Reply to receiver
                        await websocket.send(json.dumps({
                            "type": "receiver_connected",
                            "sender_ip": sender_data["sender_ip"],
                            "sender_port": sender_data["sender_port"],
                            "receiver_ip": client_ip # Receiver's IP as seen by relay
                        }))
                        logging.info(f"Receiver connected to code {code} from {client_ip}:{receiver_p2p_port}. Notifying sender.")

                        # Reply to sender
                        await sender_data["sender_ws"].send(json.dumps({
                            "type": "receiver_found",
                            "receiver_ip": client_ip, # Receiver's IP as seen by relay
                            "receiver_port": receiver_p2p_port
                        }))
                        logging.info(f"Notified sender for code {code} about receiver {client_ip}:{receiver_p2p_port}")
                    else:
                        await websocket.send(json.dumps({"type": "error", "message": "Invalid or expired code"}))
                        logging.warning(f"Receiver connection failed for code {code} from {client_ip}. Code invalid, expired, or already connected.")
                
                else:
                    logging.warning(f"Received unknown message type: {msg_type} from {client_ip}")
                    await websocket.send(json.dumps({"type": "error", "message": "Unknown message type"}))

            except json.JSONDecodeError:
                logging.error(f"Could not decode JSON from {client_ip}: {message_str}")
                await websocket.send(json.dumps({"type": "error", "message": "Invalid JSON format"}))
            except Exception as e:
                logging.error(f"Error processing message from {client_ip}: {e}", exc_info=True)
                await websocket.send(json.dumps({"type": "error", "message": f"Server error: {str(e)}"}))

    except websockets.exceptions.ConnectionClosedError:
        logging.info(f"Client {client_ip}:{client_port} disconnected.")
    except Exception as e:
        logging.error(f"Unhandled exception for client {client_ip}:{client_port}: {e}", exc_info=True)
    finally:
        # Cleanup logic
        if registered_code and registered_code in active_senders:
            logging.info(f"Sender with code {registered_code} disconnected. Cleaning up.")
            sender_entry = active_senders.pop(registered_code) # Use pop to get and remove
            receiver_ws = sender_entry.get("receiver_ws")
            if receiver_ws and receiver_ws.open:
                try:
                    await receiver_ws.send(json.dumps({"type": "info", "message": "Sender disconnected."}))
                    logging.info(f"Notified receiver for code {registered_code} about sender disconnection.")
                except websockets.exceptions.ConnectionClosed:
                    logging.info(f"Receiver for code {registered_code} already disconnected.")
        else:
            # Check if this disconnected client was a receiver for any active sender
            for code, data in list(active_senders.items()): # Iterate over a copy for safe modification
                if data.get("receiver_ws") == websocket:
                    logging.info(f"Receiver for code {code} disconnected. Notifying sender.")
                    data["receiver_ws"] = None # Mark receiver as disconnected
                    sender_ws = data.get("sender_ws")
                    if sender_ws and sender_ws.open:
                        try:
                            await sender_ws.send(json.dumps({"type": "info", "message": "Receiver disconnected."}))
                            logging.info(f"Notified sender for code {code} about receiver disconnection.")
                        except websockets.exceptions.ConnectionClosed:
                            logging.info(f"Sender for code {code} already disconnected while trying to notify about receiver.")
                    # Note: We don't remove the sender entry here, it might be reused or expire naturally.
                    # Or, we could remove it if the primary purpose is a one-time connection.
                    # For now, let's leave it to expire or for the sender to disconnect.
                    break 
        logging.info(f"Finished cleanup for client {client_ip}:{client_port}")


async def cleanup_expired_codes():
    """Periodically removes expired (unmatched) sender registrations."""
    while True:
        await asyncio.sleep(CODE_EXPIRY_DURATION / 2) # Check periodically
        current_time = time.time()
        expired_codes = []
        for code, data in active_senders.items():
            if data.get('receiver_ws') is None and (current_time - data['timestamp']) > CODE_EXPIRY_DURATION:
                expired_codes.append(code)
        
        for code in expired_codes:
            del active_senders[code]
            logging.info(f"Removed expired transfer code: {code}")

async def main():
    logging.info(f"Starting WebSocket server on {HOST}:{PORT}")
    
    # Start the cleanup task
    asyncio.create_task(cleanup_expired_codes())
    
    # Start the WebSocket server
    server = await websockets.serve(handle_client, HOST, PORT)
    
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())
