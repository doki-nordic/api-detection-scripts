
import os
import sys
import concurrent.futures
import doxmlparser
import doxmlparser.index as dox_index
import doxmlparser.compound as dox_compound
from random import shuffle
from pathlib import Path
from json import JSONEncoder
from typing import Callable, Iterable

'''
pip install doxmlparser
cd zephyr/doc
make configure
cd _build
ninja doxygen
'''

HEADER_FILE_EXTENSION = '.h'

XML_DIR = Path('zephyr/doc/_build/doxygen/xml')

class Node:
    id: str
    kind: str = ''
    name: str = ''
    file: str = ''
    line: str = ''
    parent_ids: 'set(str) | None' = None
    children_ids: 'set(str) | None' = None
    desc: str = ''
    def __init__(self, id: str, name: str):
        self.id = id
        self.name = name
    def get_short_id(self):
        return self.kind + ':' + str(self.name)
    def add_parent(self, parent: str):
        if not self.parent_ids:
            self.parent_ids = set()
        self.parent_ids.add(parent)
    def add_child(self, child: str):
        if not self.children_ids:
            self.children_ids = set()
        self.children_ids.add(child)


class File(Node):
    kind: str = 'file'

class Group(Node):
    kind: str = 'group'
    title: str = ''

class Struct(Node):
    kind: str
    is_union: bool
    def __init__(self, id: str, name: str, is_union: bool):
        super().__init__(id, name)
        self.is_union = is_union
        self.kind = 'union' if is_union else 'struct'

class Param:
    index: int
    type: str
    name: str
    desc: str

class Function(Node):
    kind: str = 'func'
    return_type: str = 'void'
    params: 'list[Param]'
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self.params = []
    def add_param(self):
        param = Param()
        param.index = len(self.params)
        self.params.append(param)
        return param

nodes: 'list[Node]' = []
nodes_by_id: 'dict(str, Node)' = {}
nodes_by_short_id: 'dict(str, Node | list[Node])' = {}


def warning(*args, **kwargs):
    args = ('\x1B[33mwarning:\x1B[0m', *args)
    print(*args, **kwargs, file=sys.stderr)


def error(*args, **kwargs):
    args = ('\x1B[31merror:\x1B[0m', *args)
    print(*args, **kwargs, file=sys.stderr)


process_executor = None
thread_executor = None


def concurrent_pool_iter(func: Callable, iterable: Iterable, use_process: bool=False,
                         threshold: int=2):
    ''' Call a function for each item of iterable in a separate thread or process.

    Number of parallel executors will be determined by the CPU count or command line arguments.

    @param func         Function to call
    @param iterable     Input iterator
    @param use_process  Runs function on separate process when True, thread if False
    @param threshold    If number of elements in iterable is less than threshold, no parallel
                        threads or processes will be started.
    @returns            Iterator over tuples cotaining: return value of func, input element, index
                        of that element (starting from 0)
    '''
    global process_executor, thread_executor, executor_workers
    collected = iterable if isinstance(iterable, tuple) else tuple(iterable)
    if len(collected) >= threshold:
        executor_workers = os.cpu_count() #args.processes if args.processes > 0 else os.cpu_count()
        if executor_workers is None or executor_workers < 1:
            executor_workers = 1
        if use_process:
            if process_executor is None:
                process_executor = concurrent.futures.ProcessPoolExecutor(executor_workers)
            executor = process_executor
        else:
            if thread_executor is None:
                thread_executor = concurrent.futures.ThreadPoolExecutor(executor_workers)
            executor = thread_executor
        chunksize = (len(collected) + executor_workers - 1) // executor_workers
        it = executor.map(func, collected, chunksize=chunksize)
    else:
        it = map(func, collected)
    return zip(it, collected, range(len(collected)))


def parse_location(node: Node, compound: 'dox_compound.compounddefType | dox_compound.memberdefType'):
    loc = compound.location
    if not loc:
        node.file = ''
        node.line = None
    elif hasattr(loc, 'bodyfile') and loc.bodyfile and loc.bodyfile.endswith(HEADER_FILE_EXTENSION):
        node.file = loc.bodyfile
        node.line = loc.bodystart if hasattr(loc, 'bodystart') else None
    elif hasattr(loc, 'file') and loc.file and loc.file.endswith(HEADER_FILE_EXTENSION):
        node.file = loc.file
        node.line = loc.line if hasattr(loc, 'line') else None
    elif hasattr(loc, 'declfile') and loc.declfile and loc.declfile.endswith(HEADER_FILE_EXTENSION):
        node.file = loc.declfile
        node.line = loc.declline if hasattr(loc, 'declline') else None
    else:
        node.file = ''
        node.line = None


def parse_description(*args):
    return '' # TODO: convert descriptions to string
    # <briefdescription>
    # <detaileddescription>
    # <inbodydescription>


def parse_type(type: 'dox_compound.linkedTextType | None') -> str:
    if not type:
        return 'void'
    result = ''
    for part in type.content_:
        part: dox_compound.MixedContainer
        if part.category == dox_compound.MixedContainer.CategoryText:
            result += part.value
        elif (part.category == dox_compound.MixedContainer.CategoryComplex) and (part.name == 'ref'):
            value: dox_compound.refTextType = part.value
            result += value.valueOf_
    return result


def parse_function(memberdef: dox_compound.memberdefType) -> Function:
    func = Function(memberdef.id, memberdef.name)
    parse_location(func, memberdef)
    func.desc = parse_description(memberdef)
    for dox_param in memberdef.param:
        dox_param: dox_compound.paramType
        param = func.add_param()
        param.desc = parse_description(dox_param)
        param.name = dox_param.declname
        param.type = parse_type(dox_param.get_type())
    func.return_type = parse_type(memberdef.get_type())
    return func

def parse_memberdef(memberdef: dox_compound.memberdefType) -> 'list[Node]':
    result: 'list[Node]' = []
    if memberdef.kind == dox_compound.DoxMemberKind.FUNCTION:
        result.append(parse_function(memberdef))
    return result


def parse_file_or_group(node: 'File | Group', compound: dox_compound.compounddefType):
    result: 'list[Node]' = [node]
    parse_location(node, compound)
    node.desc = parse_description(compound)
    for inner_ref in (compound.innerclass or []) + (compound.innergroup or []):
        inner_ref: dox_compound.refType
        node.add_child(inner_ref.refid)
    for sectiondef in compound.sectiondef or []:
        sectiondef: dox_compound.sectiondefType
        for member in sectiondef.member:
            member: dox_compound.MemberType
            node.add_child(member.refid)
        for memberdef in sectiondef.memberdef or []:
            children = parse_memberdef(memberdef)
            for child in children:
                child: Node
                node.add_child(child.id)
            result.extend(children)
    return result


def parse_file(compound: dox_compound.compounddefType) -> 'list[Node]':
    file = File(compound.id, compound.compoundname)
    return parse_file_or_group(file, compound)


def parse_group(compound: dox_compound.compounddefType) -> 'list[Node]':
    group = Group(compound.id, compound.compoundname)
    group.title = compound.title
    return parse_file_or_group(group, compound)


def parse_struct(compound: dox_compound.compounddefType, is_union: bool) -> 'list[Node]':
    struct = Struct(compound.id, compound.compoundname, is_union)
    parse_location(struct, compound)
    struct.desc = parse_description(compound)
    return struct


def process_compound(id: str) -> 'list[Node]':
    result: list[Node] = []
    for compound in dox_compound.parse(XML_DIR / (id + '.xml'), True, True).get_compounddef():
        compound: dox_compound.compounddefType
        if compound.kind == dox_index.CompoundKind.FILE:
            result.append(parse_file(compound))
        elif compound.kind == dox_index.CompoundKind.GROUP:
            result.append(parse_group(compound))
        elif compound.kind in (dox_index.CompoundKind.STRUCT,
                               dox_index.CompoundKind.CLASS,
                               dox_index.CompoundKind.UNION):
            result.append(parse_struct(compound, (compound.kind == dox_index.CompoundKind.UNION)))
        else:
            warning(f'Unexpected doxygen compound kind: "{compound.kind}"')
    return result


def parse_all(dir: Path):
    index = dox_index.parse(dir / 'index.xml', True, True)
    ids: 'list[str]' = []
    for compound in index.get_compound():
        if compound.kind in (dox_index.CompoundKind.FILE,
                             dox_index.CompoundKind.GROUP,
                             dox_index.CompoundKind.STRUCT,
                             dox_index.CompoundKind.CLASS,
                             dox_index.CompoundKind.UNION):
            ids.append(compound.refid)
        elif compound.kind in (dox_index.CompoundKind.PAGE,
                               dox_index.CompoundKind.DIR,
                               dox_index.CompoundKind.CATEGORY,
                               dox_index.CompoundKind.CONCEPT,
                               dox_index.CompoundKind.EXAMPLE):
            pass
        else:
            warning(f'Unknown doxygen compound kind: "{compound.kind}"')
    shuffle(ids)
    #ids = ids[0:100]
    for node, _, _ in concurrent_pool_iter(process_compound, ids, True, 20):
        nodes.extend(node)


if __name__ == '__main__':
    parse_all(XML_DIR)
    class MyEncoder(JSONEncoder):
        def default(self, o):
            if isinstance(o, set):
                return list(o)
            else:
                return o.__dict__
    print(MyEncoder(indent=4).encode(nodes))
