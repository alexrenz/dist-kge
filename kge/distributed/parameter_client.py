import torch
try:
    import adaps
    from adaps import Worker as LapseWorker
    from adaps import Server as LapseServer
except ImportError as e:
    from mock import Mock
    LapseWorker=Mock  # just give something to inherit from
    LapseServer=Mock  # just give something to inherit from
    pass
from typing import Optional
from torch import distributed as dist
from .parameter_server import TORCH_PARAMETER_SERVER_CMDS
from .misc import get_num_meta_keys, initialize_worker_groups, get_optimizer_dim


class KgeParameterClient:
    def __init__(
            self,
            config,
            rank,
    ):
        self.rank = rank
        embedding_dim = config.get("lookup_embedder.dim")
        optimizer_dim = get_optimizer_dim(config, embedding_dim)
        self.dim = embedding_dim + optimizer_dim
        self.num_meta_keys: int = get_num_meta_keys(config)
        self.worker_group, self.eval_worker_group = initialize_worker_groups(config, self.rank)

    def pull(self, keys, pull_tensor=None, asynchronous=False):
        raise NotImplementedError()

    def push(self, keys, push_tensor, asynchronous=False):
        raise NotImplementedError()

    def set(self, keys, set_tensor, asynchronous=False):
        raise NotImplementedError()

    def advance_clock(self):
        pass

    def current_clock(self):
        raise NotImplementedError()

    def localize(self, keys, asynchronous=False):
        raise NotImplementedError()

    def intent(self, keys, start, end=0):
        raise NotImplementedError()

    def wait(self, wait_value):
        pass

    def barrier(self):
        raise NotImplementedError()

    def shutdown(self):
        pass

    def stop(self):
        pass

    def is_stopped(self):
        return False

    @staticmethod
    def create(
        config,
        server_id,
        client_id,
        num_keys,
        server=None,
    ):
        client_type = config.get("job.distributed.parameter_server")
        if client_type == "lapse":
            return LapseParameterClient(
                config,
                server_id,
                rank=client_id,
                lapse_server=server,  # in lapse we need to provide the actual server
            )
        elif client_type == "torch":
            return TorchParameterClient(
                config=config,
                server_rank=server_id,
                rank=client_id,
                num_keys=num_keys,
            )
        elif client_type == "shared":
            return SharedParameterClient(
                config=config,
                rank=client_id,
                parameters=server,
            )
        else:
            raise ValueError(client_type)


class LapseParameterClient(LapseWorker, KgeParameterClient):
    def __init__(
        self,
        config,
        customer_id: int,
        rank: int,
        lapse_server: LapseServer,
    ):
        KgeParameterClient.__init__(self, config, rank)
        LapseWorker.__init__(self, customer_id, lapse_server)
        self.key_size = self.get_key_size()
        self._stop_key = torch.LongTensor([self.num_keys - self.num_meta_keys])
        self._optim_entity_step_key = torch.LongTensor(
            [self.num_keys - self.num_meta_keys + 1]
        )
        self._optim_relation_step_key = torch.LongTensor(
            [self.num_keys - self.num_meta_keys + 2]
        )
        self._entity_lr_key = torch.LongTensor([self.num_keys - self.num_meta_keys + 3])
        self._relation_lr_key = torch.LongTensor([self.num_keys - self.num_meta_keys + 4])
        self._stop_value_tensor = torch.zeros((1, self.key_size), dtype=torch.float32)
        self._optim_entity_step_value_tensor = torch.zeros(
            (1, self.key_size), dtype=torch.float32
        )
        self._optim_relation_step_value_tensor = torch.zeros(
            (1, self.key_size), dtype=torch.float32
        )
        self._entity_lr_tensor = torch.zeros((1, self.key_size), dtype=torch.float32)
        self._relation_lr_tensor = torch.zeros((1, self.key_size), dtype=torch.float32)
        self.meta_key_tensor = torch.zeros(
            (self.num_meta_keys, self.key_size), dtype=torch.float32
        )

    def pull(
        self, keys, pull_tensor: Optional[torch.Tensor] = None, asynchronous=False
    ):
        # if type(keys) is torch.Tensor:
        #     keys = keys.numpy.astype(np.unint64)
        if pull_tensor is None:
            pull_tensor = torch.empty([len(keys), self.key_size], dtype=torch.float32)
        return super(LapseParameterClient, self).pull(keys, pull_tensor, asynchronous)

    def push(self, keys, push_tensor: torch.Tensor, asynchronous=False):
        return super(LapseParameterClient, self).push(keys, push_tensor, asynchronous)

    def set(self, keys, set_tensor, asynchronous=False):
        super(LapseParameterClient, self).set(keys, set_tensor, asynchronous)

    def advance_clock(self):
        super(LapseParameterClient, self).advance_clock()

    def current_clock(self):
        return super(LapseParameterClient, self).current_clock()

    def localize(self, keys, asynchronous=False):
        raise NotImplementedError()

    def intent(self, keys, start, end=0):
        super(LapseParameterClient, self).intent(keys, start, end)

    def barrier(self):
        dist.barrier(group=self.worker_group)

    def barrier_eval(self):
        dist.barrier(group=self.eval_worker_group)

    def wait(self, wait_value):
        super(LapseParameterClient, self).wait(wait_value)

    def stop(self):
        super(LapseParameterClient, self).push(
            self._stop_key, torch.ones((1, self.key_size), dtype=torch.float32)
        )

    def is_stopped(self) -> bool:
        super(LapseParameterClient, self).pull(self._stop_key, self._stop_value_tensor)
        if self._stop_value_tensor[0, 0].item() == 1:
            return True
        else:
            return False

    def step_optim(self, group_name, parameter_index=0):
        super(LapseParameterClient, self).push(
            getattr(self, f"_optim_{group_name}_step_key"),
            torch.ones((1, self.key_size), dtype=torch.float32),
        )

    def get_step_optim(self, group_name, parameter_index=0):
        super(LapseParameterClient, self).pull(
            getattr(self, f"_optim_{group_name}_step_key"),
            getattr(self, f"_optim_{group_name}_step_value_tensor")
        )
        return getattr(self, f"_optim_{group_name}_step_value_tensor")[0, 0].item()

    def get_lr(self, group_name):
        super(LapseParameterClient, self).pull(getattr(self, f"_{group_name}_lr_key"),
                                               getattr(self, f"_{group_name}_lr_tensor"))
        return getattr(self, f"_{group_name}_lr_tensor")[0, 0].item()

    def set_lr(self, group_name, lr):
        getattr(self, f"_{group_name}_lr_tensor")[:] = lr
        super(LapseParameterClient, self).set(
            getattr(self, f"_{group_name}_lr_key"),
            getattr(self, f"_{group_name}_lr_tensor")
        )


class TorchParameterClient(KgeParameterClient):
    def __init__(self, config, server_rank, rank, num_keys):
        KgeParameterClient.__init__(self, config, rank)
        self.server_rank = server_rank
        self.num_keys = num_keys
        self.data_type = torch.float32
        self.lr_buffer = torch.zeros(1, dtype=torch.float32)
        self._stop_key = torch.LongTensor([self.num_keys - self.num_meta_keys])
        self._stop_value_tensor = torch.zeros((1, self.dim), dtype=torch.float32)

    def pull(self, keys, pull_tensor=None, asynchronous=False):
        cmd = torch.LongTensor([TORCH_PARAMETER_SERVER_CMDS.PULL_CMD, len(keys)])
        dist.send(cmd, dst=self.server_rank)
        dist.send(keys, dst=self.server_rank)
        if pull_tensor is None:
            pull_tensor = torch.zeros((len(keys), self.dim), dtype=self.data_type)
        dist.recv(pull_tensor, src=self.server_rank)

    def push(self, keys, push_tensor, asynchronous=False):
        cmd = torch.LongTensor([TORCH_PARAMETER_SERVER_CMDS.PUSH_CMD, len(keys)])
        dist.send(cmd, dst=self.server_rank)
        dist.send(keys, dst=self.server_rank)
        dist.send(push_tensor, dst=self.server_rank)

    def set(self, keys, set_tensor, asynchronous=False):
        cmd = torch.LongTensor([TORCH_PARAMETER_SERVER_CMDS.SET_CMD, len(keys)])
        dist.send(cmd, dst=self.server_rank)
        dist.send(keys, dst=self.server_rank)
        dist.send(set_tensor, dst=self.server_rank)

    def localize(self, keys, asynchronous=False):
        pass

    def barrier(self):
        dist.barrier(group=self.worker_group)

    def barrier_eval(self):
        dist.barrier(group=self.eval_worker_group)

    def stop(self):
        self.push(
            self._stop_key, torch.ones((1, self.dim), dtype=torch.float32)
        )

    def shutdown(self):
        cmd = torch.LongTensor([TORCH_PARAMETER_SERVER_CMDS.SHUTDOWN_CMD, 0])
        dist.send(cmd, dst=self.server_rank)

    def is_stopped(self) -> bool:
        self.pull(self._stop_key, self._stop_value_tensor)
        if torch.any(self._stop_value_tensor[0] == 1):
            return True
        else:
            return False

    def step_optim(self, group_name):
        if group_name == "entity":
            parameter_index = 0
        else:
            parameter_index = 1
        cmd = torch.LongTensor(
            [TORCH_PARAMETER_SERVER_CMDS.STEP_OPTIM_CMD, parameter_index]
        )
        dist.send(cmd, dst=self.server_rank)

    def get_step_optim(self, group_name):
        if group_name == "entity":
            parameter_index = 0
        else:
            parameter_index = 1
        cmd = torch.LongTensor(
            [TORCH_PARAMETER_SERVER_CMDS.GET_OPTIM_STEP_CMD, parameter_index]
        )
        dist.send(cmd, dst=self.server_rank)
        dist.recv(cmd, src=self.server_rank)
        return cmd[1].item()

    def get_lr(self, group_name):
        cmd = torch.LongTensor([getattr(TORCH_PARAMETER_SERVER_CMDS, f"GET_{group_name.upper()}_LR_CMD"), 0])
        dist.send(cmd, dst=self.server_rank)
        dist.recv(self.lr_buffer, src=self.server_rank)
        return self.lr_buffer[0].item()

    def set_lr(self, group_name, lr):
        cmd = torch.LongTensor([getattr(TORCH_PARAMETER_SERVER_CMDS, f"SET_{group_name.upper()}_LR_CMD"), 0])
        dist.send(cmd, dst=self.server_rank)
        self.lr_buffer[0] = lr
        dist.send(self.lr_buffer, dst=self.server_rank)


class SharedParameterClient(KgeParameterClient):
    def __init__(self, config, rank, parameters):
        KgeParameterClient.__init__(self, config, rank)
        self.parameters = parameters
        self.num_keys = len(parameters)
        self.data_type = torch.float32
        self.lr_buffer = torch.zeros(1, dtype=torch.float32)
        self._stop_key = torch.LongTensor([self.num_keys - self.num_meta_keys])
        self._optim_entity_step_key = torch.LongTensor(
            [self.num_keys - self.num_meta_keys + 1]
        )
        self._optim_relation_step_key = torch.LongTensor(
            [self.num_keys - self.num_meta_keys + 2]
        )
        self._entity_lr_key = torch.LongTensor([self.num_keys - self.num_meta_keys + 3])
        self._relation_lr_key = torch.LongTensor([self.num_keys - self.num_meta_keys + 4])
        self._stop_value_tensor = torch.zeros((1, self.dim), dtype=torch.float32)
        self._optim_entity_step_value_tensor = torch.zeros(
            (1, self.dim), dtype=torch.float32
        )
        self._optim_relation_step_value_tensor = torch.zeros(
            (1, self.dim), dtype=torch.float32
        )
        self._entity_lr_tensor = torch.zeros((1, self.dim), dtype=torch.float32)
        self._relation_lr_tensor = torch.zeros((1, self.dim), dtype=torch.float32)
        self.meta_key_tensor = torch.zeros(
            (self.num_meta_keys, self.dim), dtype=torch.float32
        )

    @torch.no_grad()
    def pull(self, keys, pull_tensor, asynchronous=False):
        pull_tensor[:, :] = self.parameters[keys, :]#.index_select(0, keys)

    @torch.no_grad()
    def push(self, keys, push_tensor, asynchronous=False):
        self.parameters[keys, :] += push_tensor
        #self.parameters.index_add_(0, keys, push_tensor)

    @torch.no_grad()
    def set(self, keys, set_tensor, asynchronous=False):
        self.parameters[keys, :] = set_tensor

    def localize(self, keys, asynchronous=False):
        pass

    def barrier(self):
        dist.barrier(group=self.worker_group)

    def barrier_eval(self):
        dist.barrier(group=self.eval_worker_group)

    def stop(self):
        self.push(
            self._stop_key, torch.ones((1, self.dim), dtype=torch.float32)
        )

    def is_stopped(self) -> bool:
        self.pull(self._stop_key, self._stop_value_tensor)
        if torch.any(self._stop_value_tensor[0] == 1):
            return True
        else:
            return False

    def step_optim(self, group_name, parameter_index=0):
        self.push(
            getattr(self, f"_optim_{group_name}_step_key"),
            torch.ones((1, self.dim), dtype=torch.float32),
        )

    def get_step_optim(self, group_name, parameter_index=0):
        self.pull(
            getattr(self, f"_optim_{group_name}_step_key"),
            getattr(self, f"_optim_{group_name}_step_value_tensor")
        )
        return getattr(self, f"_optim_{group_name}_step_value_tensor")[0, 0].item()

    def get_lr(self, group_name):
        self.pull(getattr(self, f"_{group_name}_lr_key"), getattr(self, f"_{group_name}_lr_tensor"))
        return getattr(self, f"_{group_name}_lr_tensor")[0, 0].item()

    def set_lr(self, group_name, lr):
        getattr(self, f"_{group_name}_lr_tensor")[:] = lr
        self.set(getattr(self, f"_{group_name}_lr_key"), getattr(self, f"_{group_name}_lr_tensor"))

