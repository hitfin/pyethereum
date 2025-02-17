import shutil
import tempfile
import time
from ethereum import spv
import ethereum
import ethereum.db as db
import ethereum.opcodes as opcodes
import ethereum.abi as abi
from ethereum.slogging import LogRecorder, configure_logging, set_level
from ethereum.utils import to_string
from ethereum._solidity import get_solidity
import rlp
from rlp.utils import decode_hex, encode_hex, ascii_chr

serpent = None

u = ethereum.utils
t = ethereum.transactions
b = ethereum.blocks
pb = ethereum.processblock
vm = ethereum.vm

accounts = []
keys = []

for i in range(10):
    keys.append(u.sha3(to_string(i)))
    accounts.append(u.privtoaddr(keys[-1]))

k0, k1, k2, k3, k4, k5, k6, k7, k8, k9 = keys[:10]
a0, a1, a2, a3, a4, a5, a6, a7, a8, a9 = accounts[:10]

languages = {}

_solidity = get_solidity()
if _solidity:
    languages['solidity'] = _solidity


seed = 3 ** 160


def dict_without(d, *args):
    o = {}
    for k, v in list(d.items()):
        if k not in args:
            o[k] = v
    return o


def dict_with(d, **kwargs):
    o = {}
    for k, v in list(d.items()):
        o[k] = v
    for k, v in list(kwargs.items()):
        o[k] = v
    return o


# Pseudo-RNG (deterministic for now for testing purposes)
def rand():
    global seed
    seed = pow(seed, 2, 2 ** 512)
    return seed % 2 ** 256


class ABIContract():

    def __init__(self, _state, _abi, address, listen=True, log_listener=None):
        self.address = address
        self._translator = abi.ContractTranslator(_abi)
        self.abi = _abi

        if listen:
            if not log_listener:
                listener = lambda log: self._translator.listen(log, noprint=False)
            else:
                def listener(log):
                    r = self._translator.listen(log, noprint=True)
                    if r:
                        log_listener(r)
            _state.block.log_listeners.append(listener)

        def kall_factory(f):

            def kall(*args, **kwargs):
                o = _state._send(kwargs.get('sender', k0),
                                 self.address,
                                 kwargs.get('value', 0),
                                 self._translator.encode(f, args),
                                 **dict_without(kwargs, 'sender', 'value', 'output'))
                # Compute output data
                if kwargs.get('output', '') == 'raw':
                    outdata = o['output']
                elif not o['output']:
                    outdata = None
                else:
                    outdata = self._translator.decode(f, o['output'])
                    outdata = outdata[0] if len(outdata) == 1 else outdata
                # Format output
                if kwargs.get('profiling', ''):
                    return dict_with(o, output=outdata)
                else:
                    return outdata
            return kall

        for f in self._translator.function_data:
            vars(self)[f] = kall_factory(f)


class state():

    def __init__(self, num_accounts=len(keys)):
        global serpent
        if not serpent:
            serpent = __import__('serpent')

        self.temp_data_dir = tempfile.mkdtemp()
        self.db = db.EphemDB()

        o = {}
        for i in range(num_accounts):
            o[accounts[i]] = {"wei": 10 ** 24}
        for i in range(1, 5):
            o[u.int_to_addr(i)] = {"wei": 1}
        self.block = b.genesis(self.db, start_alloc=o)
        self.blocks = [self.block]
        self.block.timestamp = 1410973349
        self.block.coinbase = a0
        self.block.gas_limit = 10 ** 9

    def __del__(self):
        shutil.rmtree(self.temp_data_dir)

    def contract(self, code, sender=k0, endowment=0, language='serpent', gas=None):
        if language not in languages:
            languages[language] = __import__(language)
        language = languages[language]
        evm = language.compile(code)
        o = self.evm(evm, sender, endowment)
        assert len(self.block.get_code(o)), "Contract code empty"
        return o

    def abi_contract(self, code, sender=k0, endowment=0, language='serpent', contract_name='',
                     gas=None, log_listener=None, listen=True):
        if contract_name:
            assert language == 'solidity'
            cn_args = dict(contract_name=contract_name)
        else:
            cn_args = {}
        if language not in languages:
            languages[language] = __import__(language)
        language = languages[language]
        evm = language.compile(code, **cn_args)
        address = self.evm(evm, sender, endowment, gas)
        assert len(self.block.get_code(address)), "Contract code empty"
        _abi = language.mk_full_signature(code, **cn_args)
        return ABIContract(self, _abi, address, listen=listen, log_listener=log_listener)


    def evm(self, evm, sender=k0, endowment=0, gas=None):
        sendnonce = self.block.get_nonce(u.privtoaddr(sender))
        tx = t.contract(sendnonce, gas_price, gas_limit, endowment, evm)
        tx.sign(sender)
        if gas is not None:
            tx.startgas = gas
        # print('starting', tx.startgas, gas_limit)
        (s, a) = pb.apply_transaction(self.block, tx)
        if not s:
            raise Exception("Contract creation failed")
        return a

    def call(*args, **kwargs):
        raise Exception("Call deprecated. Please use the abi_contract "
                        "mechanism or send(sender, to, value, "
                        "data) directly, using the abi module to generate "
                        "data if needed")

    def _send(self, sender, to, value, evmdata='', output=None,
              funid=None, abi=None, profiling=0):
        if funid is not None or abi is not None:
            raise Exception("Send with funid+abi is deprecated. Please use"
                            " the abi_contract mechanism")
        tm, g = time.time(), self.block.gas_used
        sendnonce = self.block.get_nonce(u.privtoaddr(sender))
        tx = t.Transaction(sendnonce, 1, gas_limit, to, value, evmdata)
        self.last_tx = tx
        tx.sign(sender)
        recorder = LogRecorder() if profiling > 1 else None
        (s, o) = pb.apply_transaction(self.block, tx)
        if not s:
            raise Exception("Transaction failed")
        out = {"output": o}
        if profiling > 0:
            zero_bytes = tx.data.count(ascii_chr(0))
            non_zero_bytes = len(tx.data) - zero_bytes
            intrinsic_gas_used = opcodes.GTXDATAZERO * zero_bytes + \
                opcodes.GTXDATANONZERO * non_zero_bytes
            ntm, ng = time.time(), self.block.gas_used
            out["time"] = ntm - tm
            out["gas"] = ng - g - intrinsic_gas_used
        if profiling > 1:
            trace = recorder.pop_records()
            ops = [x['op'] for x in trace if x['event'] == 'vm']
            opdict = {}
            for op in ops:
                opdict[op] = opdict.get(op, 0) + 1
            out["ops"] = opdict
        return out

    def profile(self, *args, **kwargs):
        kwargs['profiling'] = True
        return self._send(*args, **kwargs)

    def send(self, *args, **kwargs):
        return self._send(*args, **kwargs)["output"]

    def mkspv(self, sender, to, value, data=[], funid=None, abi=None):
        sendnonce = self.block.get_nonce(u.privtoaddr(sender))
        if funid is not None:
            evmdata = serpent.encode_abi(funid, *abi)
        else:
            evmdata = serpent.encode_datalist(*data)
        tx = t.Transaction(sendnonce, 1, gas_limit, to, value, evmdata)
        self.last_tx = tx
        tx.sign(sender)
        return spv.mk_transaction_spv_proof(self.block, tx)

    def verifyspv(self, sender, to, value, data=[],
                  funid=None, abi=None, proof=[]):
        sendnonce = self.block.get_nonce(u.privtoaddr(sender))
        if funid is not None:
            evmdata = serpent.encode_abi(funid, *abi)
        else:
            evmdata = serpent.encode_datalist(*data)
        tx = t.Transaction(sendnonce, 1, gas_limit, to, value, evmdata)
        self.last_tx = tx
        tx.sign(sender)
        return spv.verify_transaction_spv_proof(self.block, tx, proof)

    def trace(self, sender, to, value, data=[]):
        # collect log events (independent of loglevel filters)
        recorder = LogRecorder()
        self.send(sender, to, value, data)
        return recorder.pop_records()

    def mine(self, n=1, coinbase=a0):
        for i in range(n):
            self.block.finalize()
            self.block.commit_state()
            self.db.put(self.block.hash, rlp.encode(self.block))
            t = self.block.timestamp + 6 + rand() % 12
            x = b.Block.init_from_parent(self.block, coinbase, timestamp=t)
            # copy listeners
            x.log_listeners = self.block.log_listeners
            self.block = x
            self.blocks.append(self.block)

    def snapshot(self):
        return rlp.encode(self.block)

    def revert(self, data):
        self.block = rlp.decode(data, b.Block, db=self.db)

# logging


def set_logging_level(lvl=0):
    trace_lvl_map = [
        ':info',
        'eth.vm.log:trace',
        ':info,eth.vm.log:trace,eth.vm.exit:trace',
        ':info,eth.vm.log:trace,eth.vm.op:trace,eth.vm.stack:trace',
        ':info,eth.vm.log:trace,eth.vm.op:trace,eth.vm.stack:trace,' +
        'eth.vm.storage:trace,eth.vm.memory:trace'
    ]
    configure_logging(config_string=trace_lvl_map[lvl])
    if lvl == 0:
        set_level(None, 'info')
    print('Set logging level: %d' % lvl)


def set_log_trace(logger_names=[]):
    """
    sets all named loggers to level 'trace'
    attention: vm.op.* are only active if vm.op is active
    """
    for name in logger_names:
        assert name in slogging.get_logger_names()
        slogging.set_level(name, 'trace')


def enable_logging():
    set_logging_level(1)


def disable_logging():
    set_logging_level(0)


gas_limit = 1000000
gas_price = 1
