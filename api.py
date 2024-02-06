
import os
import sys
import re
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
    fields: 'list[StructField]'
    def __init__(self, id: str, name: str, is_union: bool):
        super().__init__(id, name)
        self.is_union = is_union
        self.kind = 'union' if is_union else 'struct'
        self.fields = []

class Param:
    index: int
    type: str
    name: str
    desc: str

class FunctionLike(Node):
    params: 'list[Param]'
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self.params = []
    def add_param(self):
        param = Param()
        param.index = len(self.params)
        self.params.append(param)
        return param

class Function(FunctionLike):
    kind: str = 'func'
    return_type: str = 'void'

class Define(FunctionLike):
    kind: str = 'def'
    value: str = ''

class EnumValue:
    index: int
    name: str
    desc: str
    value: str

class Enum(Node):
    kind: str = 'enum'
    values: 'list[EnumValue]'
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self.values = []
    def add_value(self):
        value = EnumValue()
        value.index = len(self.values)
        self.values.append(value)
        return value
    
class SimpleNode(Node):
    type: str = ''

class Typedef(SimpleNode):
    kind: str = 'typedef'

class Variable(SimpleNode):
    kind: str = 'var'

class StructField(SimpleNode):
    kind: str = 'field'
    index: int = 0



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


def parse_description(*args):
    return '' # TODO: convert descriptions to string
    # <briefdescription>
    # <detaileddescription>
    # <inbodydescription>

def parse_location_description(node: Node, compound: 'dox_compound.compounddefType | dox_compound.memberdefType'):
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
    node.desc = parse_description(compound)


def parse_linked_text(type: 'dox_compound.linkedTextType | None') -> str:
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


def parse_function_like(node: FunctionLike, memberdef: dox_compound.memberdefType):
    parse_location_description(node, memberdef)
    for dox_param in memberdef.param:
        dox_param: dox_compound.paramType
        param = node.add_param()
        param.desc = parse_description(dox_param)
        param.name = dox_param.declname or dox_param.defname
        param.type = parse_linked_text(dox_param.get_type())

def parse_function(memberdef: dox_compound.memberdefType) -> Function:
    func = Function(memberdef.id, memberdef.name)
    parse_function_like(func, memberdef)
    func.return_type = parse_linked_text(memberdef.get_type())
    return func

def parse_define(memberdef: dox_compound.memberdefType) -> Define:
    define = Define(memberdef.id, memberdef.name)
    parse_function_like(define, memberdef)
    define.value = parse_linked_text(memberdef.initializer)
    return define

def parse_enum(memberdef: dox_compound.memberdefType, name_override: str=None) -> Enum:
    enum = Enum(memberdef.id, name_override or memberdef.name)
    parse_location_description(enum, memberdef)
    for dox_value in memberdef.enumvalue:
        dox_value: dox_compound.enumvalueType
        enum_value = enum.add_value()
        enum_value.desc = parse_description(dox_value)
        enum_value.name = dox_value.name
        enum_value.value = parse_linked_text(memberdef.initializer)
    return enum

def parse_simple_node(node: SimpleNode, memberdef: dox_compound.memberdefType) -> SimpleNode:
    parse_location_description(node, memberdef)
    node.type = parse_linked_text(memberdef.get_type()) + (memberdef.argsstring or '')
    return node

def parse_memberdef(memberdef: dox_compound.memberdefType) -> 'list[Node]':
    result: 'list[Node]' = []
    if memberdef.kind == dox_compound.DoxMemberKind.FUNCTION:
        result.append(parse_function(memberdef))
    elif memberdef.kind == dox_compound.DoxMemberKind.DEFINE:
        result.append(parse_define(memberdef))
    elif memberdef.kind == dox_compound.DoxMemberKind.ENUM:
        result.append(parse_enum(memberdef))
    elif memberdef.kind == dox_compound.DoxMemberKind.TYPEDEF:
        result.append(parse_simple_node(Typedef(memberdef.id, memberdef.name), memberdef))
    elif memberdef.kind == dox_compound.DoxMemberKind.VARIABLE:
        result.append(parse_simple_node(Variable(memberdef.id, memberdef.name), memberdef))
    else:
        warning(f'Unknown member kind "{memberdef.kind}".')
    return result


def parse_file_or_group(node: 'File | Group', compound: dox_compound.compounddefType) -> 'list[Node]':
    result: 'list[Node]' = [node]
    parse_location_description(node, compound)
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


def parse_field_with_macro(memberdef: dox_compound.memberdefType) -> StructField:
    field = StructField(memberdef.id, memberdef.name)
    parse_location_description(field, memberdef)
    argsstring: str = (memberdef.argsstring or '')
    regex = r'^\s*\(\s*([a-z_0-9]+)(?:\(.*?\)|.)*?\)(?:\s*([A-Z_0-9]+)\s*$)?'
    matches = re.search(regex, argsstring, re.IGNORECASE | re.DOTALL)
    field.type = parse_linked_text(memberdef.get_type())
    if matches:
        if len(field.type):
            field.type += ' '
        field.type += field.name
        if matches.group(2):
            field.type += (argsstring[:matches.start(2)].strip() + argsstring[matches.end(2):].strip()).strip()
            field.name = matches.group(2)
        else:
            field.type += (argsstring[:matches.start(1)].strip() + argsstring[matches.end(1):].strip()).strip()
            field.name = matches.group(1)
    else:
        field.type = parse_linked_text(memberdef.get_type()) + argsstring
    return field

def parse_struct(compound: dox_compound.compounddefType, is_union: bool) -> 'list[Node]':
    result: 'list[Node]' = []
    struct = Struct(compound.id, compound.compoundname, is_union)
    parse_location_description(struct, compound)
    for sectiondef in compound.sectiondef or []:
        sectiondef: dox_compound.sectiondefType
        for memberdef in sectiondef.memberdef or []:
            memberdef: dox_compound.memberdefType
            if memberdef.kind == dox_compound.DoxMemberKind.VARIABLE:
                field: StructField = parse_simple_node(StructField(memberdef.id, memberdef.name), memberdef)
                field.index = len(struct.fields)
                struct.fields.append(field)
            elif memberdef.kind == dox_compound.DoxMemberKind.FUNCTION:
                field = parse_field_with_macro(memberdef)
                field.index = len(struct.fields)
                struct.fields.append(field)
            elif memberdef.kind == dox_compound.DoxMemberKind.ENUM:
                full_name = memberdef.qualifiedname
                if not memberdef.name:
                    full_name += '::' + memberdef.id
                enum = parse_enum(memberdef, full_name)
                result.append(enum)
            else:
                warning(f'Unknown structure member kind "{memberdef.kind}", name {memberdef.name} in {struct.name}, {struct.file}:{struct.line}')
    result.append(struct)
    return result


def process_compound(id: str) -> 'list[Node]':
    result: list[Node] = []
    for compound in dox_compound.parse(XML_DIR / (id + '.xml'), True, True).get_compounddef():
        compound: dox_compound.compounddefType
        if compound.kind == dox_index.CompoundKind.FILE:
            result.extend(parse_file(compound))
        elif compound.kind == dox_index.CompoundKind.GROUP:
            result.extend(parse_group(compound))
        elif compound.kind in (dox_index.CompoundKind.STRUCT,
                               dox_index.CompoundKind.CLASS,
                               dox_index.CompoundKind.UNION):
            result.extend(parse_struct(compound, (compound.kind == dox_index.CompoundKind.UNION)))
        else:
            warning(f'Unexpected doxygen compound kind: "{compound.kind}"')
    return result

class ParseResult:
    nodes: 'list[Node]'
    nodes_by_id: 'dict(str, Node)'
    nodes_by_short_id: 'dict(str, Node | list[Node])'
    def __init__(self):
        self.nodes = []
        self.nodes_by_id = {}
        self.nodes_by_short_id = {}


def parse_doxygen_xml(dir: Path) -> ParseResult:
    result = ParseResult()
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
        result.nodes.extend(node)
    return result


if __name__ == '__main__':
    res = parse_doxygen_xml(XML_DIR)
    class MyEncoder(JSONEncoder):
        def default(self, o):
            if isinstance(o, set):
                return list(o)
            else:
                d = {}
                for name in tuple(dir(o)):
                    if not name.startswith('_'):
                        value = getattr(o, name)
                        if not callable(value):
                            d[name] = value
                return d
    print(MyEncoder(sort_keys=False, indent=4).encode(res.nodes))
