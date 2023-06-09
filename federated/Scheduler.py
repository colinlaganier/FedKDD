import math
import random
import torch
import torch.nn as nn
import torch.optim as optim
import torch.multiprocessing as mp
from torch.utils.data import DataLoader, TensorDataset
from queue import Queue
import Client, Dataset, Server
from knowledge_distillation import Logits, SoftTarget

class Scheduler:

    def __init__(self, 
                 num_clients, 
                 num_devices, 
                 server_model, 
                 client_models, 
                 epochs, 
                 batch_size, 
                 kd_batch_size,
                 num_rounds, 
                 data_path, 
                 dataset_id, 
                 data_partition, 
                 load_diffusion,
                 logger):

        self.logger = logger

        self.clients = [None] * num_clients
        self.device_dict = dict(zip(range(num_devices), [[] for _ in range(num_devices)]))
        self.num_devices = num_devices
        self.num_clients = num_clients
        self.server_device = None
        self.server_model = server_model
        self.client_devices = None
        self.client_models = client_models

        # Training parameters
        self.round = 0
        self.num_rounds = num_rounds
        self.train_epochs = epochs
        self.batch_size = batch_size
        self.eval_seed = random.randint(0, 100000)

        # model and dataset dependant
        self.training_params = {"num_classes": 10,
                                "optimizer": optim.SGD, 
                                "criterion": nn.CrossEntropyLoss, 
                                "lr": 0.1 , 
                                "momentum": 0.9, 
                                "epochs": self.train_epochs,
                                "weight_decay": 1e-4, 
                                "kd_optimizer": optim.SGD,
                                "kd_criterion": SoftTarget,
                                "kd_lr": 0.1,
                                "kd_momentum": 0.9,
                                "kd_alpha": 0.5,
                                "kd_temperature": 1,
                                "kd_epochs": 10,
                                "eval_seed": self.eval_seed}

        # Setup datasets
        self.dataset = Dataset(data_path, dataset_id, batch_size, kd_batch_size, num_clients)
        self.dataset.prepare_data(data_partition)

        # If single synthetic dataset
        self.synthetic_dataset = self.dataset.get_synthetic_data(None)

        if load_diffusion:
            self.dataset.set_synthetic_data()
        
        # Assign devices to server and clients based on number of devices
        self.assign_devices()

        # Setup server and initialize
        self.server = Server(self.server_device, self.server_model(), self.training_params, self.logger)
        self.server.init_server()

        # Setup clients and initialize
        self.setup_clients()
        self.init_clients()

    def assign_devices(self):
        """
        Assigns GPU device id to clients
        """
        # Multi GPU 
        if (self.num_devices > 1):
            # Find optimal device assignment for balance
            num_devices_avail = self.num_devices
            avg_num_clients = (self.num_clients + 1) // num_devices_avail
            remainder = (self.num_clients + 1) % num_devices_avail

            client_devices = []
            start = 0

            for i in range(num_devices_avail):
                if remainder > 0:
                    end = start + avg_num_clients + 1
                    remainder -= 1
                else:
                    end = start + avg_num_clients

                client_devices.extend([i] * (end - start))
                start = end

            self.server_device = client_devices[0]
            self.client_devices = client_devices[1:]            
        # Single GPU
        else:
            # Assign all clients to the same device
            self.server_device = 0
            self.client_devices = [0] * self.num_clients

    def setup_clients(self):
        """
        Initialize client objects
        """
        for client_id in range(self.num_clients):            
            self.clients[client_id] = Client(client_id, 
                                        self.client_devices[client_id], 
                                        self.client_models[client_id](), 
                                        self.dataset.client_dataloaders[client_id],
                                        self.logger)        
            
            # Assign client to device in device_dict
            self.device_dict[self.client_devices[client_id]].append(client_id)

    def init_clients(self):
        # train each client on local data 
        for client in self.clients:
            client.init_model(self.training_params)
        pass

    def train(self, num_rounds, logit_ensemble=True):
        """
        Train client models on local data and perform co-distillation
        
        Args:
            num_rounds (int): number of rounds to train
            train_epochs (int): number of epochs to train each client
        """
        logit_queue = Queue()

        for round in num_rounds:
            self.round += 1

            # Generate server logit
            if self.load_diffusion:
                # synthetic_dataset = self.dataset.get_synthetic_data(round)
                synthetic_dataset = self.synthetic_dataset
                diffusion_seed = None
                server_logit = self.server.generate_logit(synthetic_dataset, diffusion_seed)
            else:
                synthetic_dataset = None
                diffusion_seed = self.server.generate_seed()
                server_logit = self.server.generate_logit(synthetic_dataset, diffusion_seed)

            # Create dataloader for server logit
            server_logit = DataLoader(TensorDataset(server_logit), batch_size=128)
            # if (self.num_devices > 1):
            #     # Distribute server logit to clients DDP?
   
            # train each client in parallel
            for client in self.clients:
                # Train client on local data
                client.train()
                client.evaluate(self.dataset.test_dataloader)

                # Knowledge distillation with server logit and synthetic diffusion data
                # if self.load_diffusion:
                #     client.set_synthetic_dataset(synthetic_dataset)
                client.knowledge_distillation(server_logit, synthetic_dataset, diffusion_seed)
                client.evaluate(self.dataset.test_dataloader)

                # Generate logit for server update
                client_logit = client.generate_logit(synthetic_dataset, diffusion_seed)
                logit_queue.put(client_logit)

                # client.save_checkpoint()

            if logit_ensemble:
                # Average client logits
                num_clients = logit_queue.qsize()
                logit_sum = torch.zeros_like(client_logit)
                while not logit_queue.empty():
                    logit_sum += logit_queue.get()
                logit_queue.put(logit_sum / num_clients)      

            # Update server model with client logits
            self.server.knowledge_distillation(logit_queue, synthetic_dataset, diffusion_seed)
            self.server.evaluate(self.dataset.test_dataloader)


    def client_update(self, client, server_logit, diffusion_seed, logit_queue):
        # Train client on local data
        client.train(self.train_epochs)
        # Knowledge distillation with server logit and synthetic diffusion data
        client.knowledge_distillation(server_logit, diffusion_seed)
        # Generate logit for server update
        client_logit = client.generate_logit(diffusion_seed)
        logit_queue.put(client_logit)

    def train_mp(self, num_rounds, train_epochs):

        # Initialize logit queue for server update after each round
        logit_queue = Queue()

        for _ in num_rounds:
            self.round += 1

            diffusion_seed = self.server.generate_seed()
            server_logit = self.server.get_logit()
            processes = [] 

            # Start processes for each client on each device
            for i in range(math.ceil(self.num_clients / self.num_devices)):
                for device, client_ids in self.device_dict.items():
                    if i < len(client_ids):
                        process = mp.Process(target=self.client_update, args=(self.clients[client_ids[i]], server_logit, diffusion_seed, logit_queue))
                        process.start()
                        processes.append(process)

            # Wait for all processes to finish
            for process in processes:
                process.join()

            # Update server model with client logit queue
            self.server.knowledge_distillation(logit_queue)
