#!/usr/bin/env python
"""
PyCscope

PyCscope creates a Cscope-like index file for a tree of Python source.
"""


__author__ = "Dean Hall (dwhall256\x40yahoo\x2Ecom)"
__copyright__ = "Copyright 2006 Dean Hall.  See LICENSE for details."
__date__ = "2008/10/07"
__version__ = "0.3a-pajp"
__usage__ = """Usage: pycscope.py [-R] [-t cnt] [-f reffile] [-i srclistfile] [files ...]

-R              Recurse directories for files.
-D              Dump the AST parse tree for each file
-t              Use cnt threads (defaults to 1).
-f reffile      Use reffile as cross-ref file name instead of cscope.out.
-i srclistfile  Use a file that contains a list of source files to scan."""


import getopt, sys, os, os.path, string, types
import keyword, parser, symbol, token, compiler
from threading import Lock, Thread

class Mark(object):
    """ Marks, as defined by Cscope, that are implemented.
    """
    FILE = "@"
    FUNC_DEF = "$"
    FUNC_CALL = "`"
    FUNC_END = "}"
    INCLUDE = "~"       # TODO: assume all includes are global for now
    ASSIGN = "="        # Direct assignment, increment, or decrement
    CLASS = "c"         # Class definition
    GLOBAL = "g"        # Other global definition
    LOCAL = "l"         # Function/block local definition

    # Class private list of valid marks
    __valid = (FILE, FUNC_DEF, FUNC_CALL, FUNC_END, INCLUDE, ASSIGN, CLASS, GLOBAL, LOCAL)

    def __init__(self, mark=''):
        """ Constructor, making sure a given mark is valid.
        """

        if mark:
            assert mark in Mark.__valid, "Not a valid mark (%s)" % mark
        self.__mark = (mark or '')  # Turn None into ''

    def __eq__(self, other):
        return self.__mark == other.__mark

    def __ne__(self, other):
        return self.__mark != other.__mark

    def format(self):
        """ Marks are represented as a string with a tab character
            followed by the mark character itself, if it has
            one. Otherwise it is an empty string.
        """
        if self.__mark:
            return "\t%s" % self.__mark
        else:
            return self.__mark
    __str__ = format

    def __repr__(self):
        return "<Mark:%s>" % self.format().replace("\t", "\\t")

    def __getattr__(self, name):
        """ Used as a way for tests to check the internal value
            without exposing its name directly.
        """
        if name == '_test_mark':
            return self.__mark
        else:
            raise AttributeError(name)

markFuncEnd = Mark(Mark.FUNC_END)

# Get the list of Python keywords and add a few common builtins
kwlist = keyword.kwlist
kwlist.extend(("True", "False", "None"))

debug = False

def main():
    """Parse command line args and act accordingly.
    """
    # Parse the command line arguments
    try:
        opts, args = getopt.getopt(sys.argv[1:], "Rf:i:t:")
    except getopt.GetoptError:
        print __usage__
        sys.exit(2)
    recurse = False
    indexfn = "cscope.out"
    threadCount = 1
    for o, a in opts:
        if o == "-D":
            debug = True
        if o == "-R":
            recurse = True
        if o == "-t":
            try:
                val = int(a)
                if val > threadCount:
                    threadCount = val
            except Exception, e:
                print __usage__
                sys.exit(2)
        if o == "-f":
            indexfn = a
        if o == "-i":
            args.extend(map(string.rstrip, open(a, 'r').readlines()))

    # Search current dir by default
    if len(args) == 0:
        args = "."

    # Parse the given list of files/dirs
    basepath = os.getcwd()
    gen = genFiles(basepath, args, recurse)

    if threadCount > 1:
        # List of threads
        T = []

        indexBuffs = []
        fnamesBuffs = []

        # The lock that will be used to synchronize all the threads.
        lock = Lock()

        # Create a pool of threads to run the following code:
        # ctx = (gen, basepath, lock)
        # return (indexbuff, fnamesbuff)
        for i in range(threadCount - 1):
            t = WorkerThread(basepath, gen, lock, indexBuffs, fnamesBuffs)
            t.setName("Worker-%d" % i)
            T.append(t)
            t.start()

        # Have the main thread contribute as best it can with the other threads
        doWork = workT
    else:
        # No threads
        lock = None
        doWork = work

    indexbuff, fnamesbuff = doWork(basepath, gen, lock)

    if threadCount > 1:
        for t in T:
            t.join()

        # Gather all the indexbuffs and fnamesbuffs 
        for i in indexBuffs:
            indexbuff += i
        for f in fnamesBuffs:
            fnamesbuff += f

    # Symbol data for the last file ends with a file mark
    indexbuff.append("\n%s" % Mark(Mark.FILE))
    writeIndex(basepath, indexfn, indexbuff, fnamesbuff)

class WorkerThread(Thread):
    def __init__(self, basepath, gen, lock, indexBuffs, fnamesBuffs):
        Thread.__init__(self, name="Worker")
        self.basepath = basepath
        self.gen = gen
        self.lock = lock
        self.ibs = indexBuffs
        self.fnms = fnamesBuffs

    def run(self):
        indexbuff, fnamesbuff = workT(self.basepath, self.gen, self.lock)

        # Add our results to the overall list
        self.lock.acquire()
        self.ibs.append(indexbuff)
        self.fnms.append(fnamesbuff)
        self.lock.release()


def work(basepath, gen, lock):
    """ The actual work the ONE thread performs. We do not make this a
        method of the WorkerThread class since it will be used when no
        threads are selected (the default).
    """

    # Create the buffer to store the output (list of strings)
    indexbuff = []
    indexbuff_len = 0
    fnamesbuff = []

    for fname in gen:
        try:
            indexbuff_len = parseFile(basepath, fname, indexbuff, indexbuff_len, fnamesbuff)
        except SyntaxError, se:
            print "pycscope.py: %s: %s" % (se.filename, se)
            pass

    return indexbuff, fnamesbuff


def workT(basepath, gen, lock):
    """ The actual work the threads will perform. We do not make this a
        method of the WorkerThread class so that the main thread can use
        this to participate as well.
    """

    # Create the buffer to store the output (list of strings)
    indexbuff = []
    indexbuff_len = 0
    fnamesbuff = []

    lock.acquire()
    for fname in gen:
        lock.release()
        try:
            indexbuff_len = parseFile(basepath, fname, indexbuff, indexbuff_len, fnamesbuff)
        except SyntaxError, se:
            print "pycscope.py: %s: %s" % (se.filename, se)
            pass
        lock.acquire()
    lock.release()

    return indexbuff, fnamesbuff


def isPython(name):
    # Is this a python file?
    return name[-3:] == ".py"


def genFiles(basepath, args, recurse):
    """ A generator for returning all the files that need to be parsed.
        Caller is required to provide synchronization.
    """
    for name in args:
        if os.path.isdir(os.path.join(basepath, name)):
            for fname in parseDir(basepath, name, recurse):
                yield fname
        else:
            # Don't return the file name if it's not python source
            if isPython(name):
                yield name


def parseDir(basepath, relpath, recurse):
    """ A generator that parses all files in the directory and
        recurses into subdirectories if requested.
        Caller is required to provide synchronization.
    """
    dirpath = os.path.join(basepath, relpath)
    for name in os.listdir(dirpath):
        fullpath = os.path.join(dirpath, name)
        if os.path.isdir(fullpath) and recurse:
            for fname in parseDir(basepath, os.path.join(relpath, name), recurse):
                yield fname
        else:
            if isPython(name):
                yield os.path.join(relpath, name)


def parseFile(basepath, relpath, indexbuff, indexbuff_len, fnamesbuff):
    """Parses a source file and puts the resulting index into the buffer.
       Caller is required to provide synchronization.
    """
    # Open the file and get the contents
    fullpath = os.path.join(basepath, relpath)
    f = open(fullpath, 'rU')
    filecontents = f.read()
    f.close()

    # Add the file mark to the index
    fnamesbuff.append(relpath)
    indexbuff.append("\n%s%s\n\n" % (Mark(Mark.FILE), relpath))
    indexbuff_len += 1

    # Add path info to any syntax errors in the source files
    if filecontents:
        try:
            indexbuff_len = parseSource(filecontents, indexbuff, indexbuff_len)
        except SyntaxError, se:
            se.filename = fullpath
            raise se

    return indexbuff_len

nodeNames = token.tok_name
nodeNames.update(symbol.sym_name)

def replaceNodeType(treeList):
    """ Replaces the 0th element in the list with the name
        that corresponds to its node value.
    """
    global nodeNames

    # Replace node num with name
    treeList[0] = nodeNames[treeList[0]]

    # Recurse
    for i in range(1, len(treeList)):
        if type(treeList[i]) == types.ListType:
            replaceNodeType(treeList[i])
    return treeList

def dumpAst(ast):
    import pprint
    pprint.pprint(replaceNodeType(ast.tolist(True)))

class Symbol(object):
    """ A representation of a what cscope considers a 'symbol'.
    """
    def __init__(self, name, mark=None):
        """ Constructor, which ensures an actual name ("string") is given.
        """
        assert (mark == Mark.FUNC_END or name) and (type(name) == types.StringType), "Must have an actual symbol name as a string (unless marking function end)."

        self.__mark = Mark(mark)
        self.__name = name

    def __add__(self, other):
        """ Add text to the stored name.
        """
        assert other and (isinstance(other, Symbol)), "Must have another Symbol object to concatenate."
        if self.__mark != other.__mark:
            import pdb; pdb.set_trace()
        assert self.__mark == other.__mark, "Symbols must be marked the same."
        self.__name += other.__name
        return self
    __iadd__ = __add__

    def format(self):
        """ Explicitly format the values of this object for inclusion
            in the cscope database; for symbols, an optional mark
            precedes it.
        """
        return "%s%s" % (self.__mark, self.__name)
    __str__ = format

    def __repr__(self):
        return "<Symbol:%s>" % self.format()

    def __getattr__(self, name):
        """ Used as a way for tests to check the internal value
            without exposing its name directly.
        """
        if name == '_test_mark':
            return self.__mark._test_mark
        elif name == '_test_name':
            return self.__name
        else:
            print "Symbol(): does not have attribute <%s>" % name
            raise AttributeError(name)

    def __coerce__(self, other):
        """ We do not implement coercion; we define this routine so
            that the interpretter won't invoke __getattr__() to try to
            find it.
        """
        return NotImplemented

    def __nonzero__(self):
        """ Defined so that the interpretter won't invoke
            __getattr__() to try to find it.
        """
        return True

    def hasMark(self, mark):
        """ Does this symbol have a given mark?
        """
        return self.__mark == mark

class NonSymbol(object):
    """ A representation of a what cscope considers a 'non-symbol' text.
    """
    def __init__(self, val):
        """ Constructor, whatever we are given we'll store it as a string.
        """
        assert val and (type(val) == types.StringType), "Must have an actual string."
        self.__text = str(val)

    def __add__(self, other):
        """ Add text to the stored string.
        """
        assert other and (isinstance(other, NonSymbol)), "Must have another NonSymbol object to concatenate."
        self.__text += ' ' + other.__text
        return self

    def format(self):
        """ Explicitly format the value of this object for inclusion
            in the cscope database; for non-symbol text it is just the
            stored text itself (as is).
        """
        return self.__text
    __str__ = format

    def __repr__(self):
        return "<NonSymbol:%s>" % self.format()

class Line(object):
    def __init__(self, num):
        assert ((type(num) == types.IntType) or (type(num) == types.LongType)) and num > 0, "Requires a positive, non-zero integer for a line number"
        self.lineno = num
        self.__contents = []	# List of Symbol and NonSymbol objects
        self.__hasSymbol = False

    def __getattr__(self, name):
        """ Used as a way for tests to check the internal value
            without exposing its name directly.
        """
        if name == '_test_contents':
            return self.__contents
        elif name == '_test_hasSymbol':
            return self.__hasSymbol
        else:
            print "Line(): does not have attribute <%s>" % name
            #import pdb; pdb.set_trace()
            raise AttributeError(name)

    def __add__(self, other):
        ''' Add a Symbol() or a NonSymbol() to the contents of this line
        '''
        assert isinstance(other, Symbol) or isinstance(other, NonSymbol), "Can only add Symbol or NonSymbol objects"

        global markFuncEnd

        if self.__contents and isinstance(other, Symbol) and other.hasMark(markFuncEnd):
            # If we have a function end marker, then we need to make
            # sure it is preceded by a NonSymbol to preserve
            # alternating lines of NonSymbol and then Symbol.
            if isinstance(self.__contents[-1], Symbol):
                self.__contents.append(NonSymbol(' '))
            self.__contents.append(other)
        elif self.__contents \
                and ((isinstance(self.__contents[-1], NonSymbol) and isinstance(other, NonSymbol))
                     or (isinstance(self.__contents[-1], Symbol) and isinstance(other, Symbol))):
            self.__contents[-1] += other
        else:
            if isinstance(other, Symbol):
                self.__hasSymbol = True
            self.__contents.append(other)
        return self
    __iadd__ = __add__

    def format(self):
        """ Format this source line (that has a symbol) as individual
            strings representing lines in the Cscope database.
        """
        if not self.__hasSymbol:
            return ''

        buff = []
        # Handle the formatting of the initial line number
        item = self.__contents[0]
        if isinstance(item, Symbol):
            # The line number must be placed on its own line, with a
            # trailing blank, when followed by a symbol
            buff.append("%d " % self.lineno)
            buff.append(item.format())
        else:
            assert isinstance(item, NonSymbol)
            # The line number must be placed on the same line as
            # non-symbol text following it
            buff.append("%d %s" % (self.lineno, item.format()))

        # The rest of the contents of the source line are just added
        # as individual lines (strings), preceded by a space
        for i in range(1, len(self.__contents)):
            item = self.__contents[i]
            if isinstance(item, Symbol):
                s = item.format()
                # Add a space to the NonSymbol line so that it
                # displays properly in cscope (only if it doesn't have
                # on already).
                if buff[-1] != ' ':
                    buff[-1] += ' '
            else:
                assert isinstance(item, NonSymbol)
                # Insert a space to the NonSymbol to separate it from
                # the previous Symbol line so that it displays
                # properly in cscope (only if it is not a space
                # itself).
                s = item.format()
                if s != ' ':
                    s = ' ' + s
            buff.append(s)

        # Place each string on its own line, ending the last string
        # with a new line and adding an empty line, per the Cscope
        # spec.
        return "\n".join(buff) + "\n\n"
    __str__ = format

    def __repr__(self):
        return "<Line:%s>" % self.format().replace("\n", "\\n")

    def __coerce__(self, other):
        """ We do not implement coercion; we define this routine so
            that the interpretter won't invoke __getattr___() to try to
            find it.
        """
        return NotImplemented

class Context(object):
    ''' Object representing the context for understanding the AST tree during
        one single pass.

        The buffer of Line objects with at least one symbol is maintained
        here. The current line is represented as a Line object, where it is
        saved to the buffer if it has at least one Symbol in it.

        This object also maintained a bunch of state to properly interpret AST
        entries as they are encountered.

        Cscope uses Marks to help it understand what a symbol is for. As the
        AST tree is processed, often we'll look ahead into the AST tree to
        associate a Mark with a Symbol before we have processed that
        Symbol. The dictionary of Marks encapsulates that state.
    '''
    # Buffer of lines in the Cscope database (individual strings in a list)
    def __init__(self):
        self.buff = []              # The accumlated list of lines with symbols
        self.line = Line(1)         # The current line being processed
        self.marks = {}             # Association of AST tuples to a Marks
        self.mark = ''              # The Mark to be applied to the next symbol
        self.indent_lvl = 0         # Indentation level, used to track outer fn
        self.equal_cnt = 0          # Number of equal signs expected for assgns
        self.assigned_cnt = 0       # Number of assignments taken place
        self.dotted_cnt = 0         # Number of dots to expect
        self.import_cnt = 0         # Number of import statements to expect
        self.func_def_lvl = -1      # Function definition level, to track outer
        self.import_stmt = False    # Handling an import statement (FIXME)
        self.decorator = False      # Handling a decorator (FIXME)

    def setMark(self, tup, mark):
        ''' Add a mark to the dictionary for the given tuple
        '''
        assert tup not in self.marks
        assert tup[0] == token.NAME
        self.marks[tup] = mark

    def getMark(self, tup):
        ''' Get the mark associated with the given tuple. This is a one shot
            deal, as we delete the association from the dictionary to prevent
            unnecessary accumlation of these associations given we never
            rewalk the tree (one pass only).
        '''
        assert tup in self.marks
        mark = self.marks[tup]
        del(self.marks[tup])
        return mark

    def commit(self, lineno=None):
        ''' Commit a processed souce line to the buffer
        '''
        line = str(self.line)
        if line:
            self.buff.append(line)
        if lineno:
            self.line = Line(lineno)
        else:
            self.line = None

def isNamedFuncCall(ast):
    """ Figure out if this AST sub-tree represents a named function call;
        that is, one which looks like name(), or name(arg,arg=1).
    """
    if len(ast) < 3:
        return False

    return (ast[1][0] == symbol.atom) \
            and (ast[1][1][0] == token.NAME) \
            and (ast[2][0] == symbol.trailer) \
            and (ast[2][1][0] == token.LPAR) \
            and (ast[2][-1][0] == token.RPAR)

def isTrailorFuncCall(ast):
    """ Figure out if this AST sub-tree represents a trailer name function
        call; that is, one which looks like name.name(), or
        name.name(arg,arg=1).
    """
    if len(ast) < 4:
        return False

    return (ast[-2][0] == symbol.trailer) \
            and (ast[-2][1][0] == token.DOT) \
            and (ast[-2][2][0] == token.NAME) \
            and (ast[-1][0] == symbol.trailer) \
            and (ast[-1][1][0] == token.LPAR) \
            and (ast[-1][-1][0] == token.RPAR)

def processNonTerminal(ctx, ast):
    """ Process a given AST tuple representing a non-terminal symbol
    """
    # We have a non-terminal "symbol"
    if ast[0] == symbol.global_stmt:
        # Handle global declarations
        for i in range(2, len(ast)):
            if not i % 2:
                # Even indices are the names
                assert ast[i][0] == token.NAME
                ctx.setMark(ast[i], Mark.GLOBAL)
    elif ast[0] == symbol.funcdef:
        if ctx.func_def_lvl == -1:
            # Handle function definitions. NOTE: we only mark the
            # outer most function name as a function definition
            # since the cscope utility can't handle nested
            # functions. So all nested function definitions will
            # not be marked as such.
            ctx.func_def_lvl = ctx.indent_lvl
            idx = 1
            if ast[idx][0] == symbol.decorators:
                # Skip the optional decorators
                idx += 1
            assert (ast[idx][0] == token.NAME) and (ast[idx][1] == 'def')
            idx += 1
            ctx.setMark(ast[idx], Mark.FUNC_DEF)
    if ast[0] == symbol.decorator:
        # Handle decorators.
        ctx.decorator = True
    elif ast[0] == symbol.import_stmt:
        # Handle various kinds of import statements (from ...; import ...)
        ctx.import_stmt = True # FIX-ME
    elif ctx.import_stmt and (ast[0] == symbol.dotted_as_names):
        # Figure out how many imports there are
        ctx.import_cnt = len(ast)/2
        # FIXME: Handle import counts correctly: when do we decrement import_cnt?
    elif ast[0] == symbol.dotted_name:
        # Handle dotted names
        if ctx.import_stmt:
            # For imports, we want to collect them all together to form
            # one symbol. We get a count of names coming by dividing the
            # number of elements in this AST by two: a NAME is always
            # preceded by something, either symbol.dotted_name or by
            # token.DOT
            ctx.dotted_cnt = len(ast)/2
        elif ctx.decorator:
            # Decorators use dotted names, but we don't want to consider
            # the entire sequence as the function being called since the
            # functions are not defined that way. Instead, we only mark
            # the last symbol in the sequence as being a function call.
            ctx.setMark(ast[-1], Mark.FUNC_CALL)
            # FIX-ME: Should we not be clearing the decorator context?
    elif ast[0] == symbol.expr_stmt:
        # Look for assignment statements
        #   testlist, EQUAL, testlist [, EQUAL, testlist, ...]
        l = len(ast)
        if (l >= 4):
            if (ast[1][0] == symbol.testlist) and (ast[2][0] == symbol.augassign) and (ast[3][0] == symbol.testlist):
                # testlist, augassign, testlist
                assert (ctx.mark == '' and ctx.equal_cnt == 0 and ctx.assigned_cnt == 0), \
                        "Nested augmented assignment statement (mark: %s, equal_cnt: %d, assigned_cnt: %d)?" \
                        % (ctx.mark, ctx.equal_cnt, ctx.assigned_cnt)
                ctx.mark = Mark.ASSIGN
                ctx.equal_cnt = ctx.assigned_cnt = 1
            elif (ast[1][0] == symbol.testlist) and (ast[2][0] == token.EQUAL):
                # testlist, EQUAL, ...
                assert (ctx.mark == '' and ctx.equal_cnt == 0 and ctx.assigned_cnt == 0), \
                        "Nested assignment statement (mark: %s, equal_cnt: %d, assigned_cnt: %d)?" \
                        % (ctx.mark, ctx.equal_cnt, ctx.assigned_cnt)
                ctx.mark = Mark.ASSIGN
                ctx.equal_cnt = 1
                for i in range(3, l):
                    if ast[i][0] == token.EQUAL:
                        ctx.equal_cnt += 1
                    else:
                        assert ast[i][0] == symbol.testlist, "Bad form: "
                ctx.assigned_cnt = ctx.equal_cnt
    elif ast[0] == symbol.classdef:
        # Handle class declarations.
        assert (ast[1][0] == token.NAME) and (ast[1][1] == 'class')
        ctx.setMark(ast[2], Mark.CLASS)
    elif ast[0] == symbol.power:
        if isNamedFuncCall(ast):
            # Simple named functional call like: name() or name(a,b=1,c)
            ctx.setMark(ast[1][1], Mark.FUNC_CALL)
        if isTrailorFuncCall(ast):
            # Handle named function calls like: name.name() or
            # name.name(a,b=1,c)
            ctx.setMark(ast[-2][2], Mark.FUNC_CALL)

def processTerminal(ctx, ast):
    """ Process a given AST tuple representing a terminal symbol
    """
    global kwlist

    if ast[0] == token.DEDENT:
        # Indentation is not recorded, but still processed. A
        # dedent is handled before we process any line number
        # changes so that we can properly mark the end of a
        # function.
        ctx.indent_lvl -= 1
        if ctx.indent_lvl == ctx.func_def_lvl:
            ctx.func_def_lvl = -1
            ctx.line += Symbol('', Mark.FUNC_END)
        return

    # Remember on what line this terminal symbol ended
    lineno = int(ast[2])
    if (lineno != ctx.line.lineno) and (ast[0] != token.STRING):
        # Handle a token on a new line without seeing a NEWLINE
        # token (line continuation with backslash). Skip this for
        # STRINGs so that a display utility can display Python
        # multi-line strings.
        ctx.commit(lineno)

    # Handle tokens
    if ast[0] == token.NEWLINE:
        # Handle new line tokens: we ignore them as a change in
        # the line number for a token will commit a line (or EOF,
        # see below).
        pass
    elif ast[0] == token.INDENT:
        # Indentation is not recorded, but still processed
        ctx.indent_lvl += 1
    elif ast[0] == token.STRING:
        # Handle strings: make sure newline's within strings are
        # escaped.
        ctx.line += NonSymbol(ast[1].replace("\n", "\\n"))
    elif ast[0] == token.EQUAL:
        # Handle assignment statements. Here, any user defined
        # symbol before the equal sign should be marked
        # appropriately as being "assigned to".
        ctx.line += NonSymbol(ast[1])
        if (ctx.mark == Mark.ASSIGN) and (ctx.equal_cnt >= 1):
            ctx.equal_cnt -= 1
            if ctx.equal_cnt == 0:
                assert ctx.assigned_cnt == 0, "Assignments were not all made"
                # Remove the assignment marker since there are no more
                # equal signs in this sequence.
                ctx.mark = ''
    elif ((ast[0] >= token.PLUSEQUAL) and (ast[0] <= token.DOUBLESTAREQUAL)) or (ast[0] == token.DOUBLESLASHEQUAL):
        # Handle augmented assignment statements.
        assert ctx.mark == Mark.ASSIGN, "Wrong marker (mark: %s)?" % ctx.mark
        # "There can be only one!" -Kergen, Highlander, 1986
        assert (ctx.equal_cnt == 1 and ctx.assigned_cnt == 0), \
                "Can't have nested augmented assignment statements (equal_cnt: %d, assigned_cnt: %d)" \
                % (ctx.equal_cnt, ctx.assigned_cnt)
        ctx.line += NonSymbol(ast[1])
        ctx.equal_cnt = 0
        ctx.mark = ''
    elif ast[0] == token.COMMA:
        # Comma tokens are always added to the line
        ctx.line += NonSymbol(ast[1])
        # If we are dealing with an assignment, we have encountered a
        # comma which means the symbol following is also being assigned
        # to, so we need to bump the assigned count back up to properly
        # handle that below.
        if ctx.mark == Mark.ASSIGN:
            assert ctx.equal_cnt > 0, "Comma encountered, but assignment marker in place without an equal count"
            assert (ctx.assigned_cnt + 1) == ctx.equal_cnt, "Comma encountered, but an assignment has not already taken place"
            ctx.assigned_cnt += 1
    elif ast[0] == token.NAME:
        # Handle terminal names, could be a python keyword or
        # user defined symbol, or part of a dotted name sequence.
        if ctx.dotted_cnt > 0:
            # The name is part of a dotted_name sequence from an
            # import. Decrement the count of names in the sequence
            # as we are consuming one now.
            ctx.line += Symbol(ast[1], Mark.INCLUDE)
            ctx.dotted_cnt -= 1
        elif ast[1] in kwlist:
            # Python keywords are treated as non-symbol text
            ctx.line += NonSymbol(ast[1])
        else:
            # Not a python keyword, symbol text
            if ast in ctx.marks:
                s = Symbol(ast[1], ctx.getMark(ast))
            elif ctx.mark:
                if (ctx.mark == Mark.ASSIGN):
                    # Don't consume the marker if it is an
                    # assignment type, since there can be multiple
                    # symbols for an assignment.
                    if ctx.equal_cnt == ctx.assigned_cnt:
                        # The first symbol encountered for assignments is
                        # the only one marked.
                        s = Symbol(ast[1], ctx.mark)
                        ctx.assigned_cnt -= 1
                    else:
                        # Subsequent symbols encountered before the
                        # following assignment are not marked.
                        s = Symbol(ast[1])
                elif ctx.mark == Mark.INCLUDE:
                    # Don't consume the marker for includes
                    # either, as this could be a NAME in a dotted
                    # name sequence.
                    s = Symbol(ast[1], ctx.mark)
                else:
                    s = Symbol(ast[1], ctx.mark)
                    # Consume the mark since it only applies to the first
                    # symbol encountered.
                    ctx.mark = ''
            else:
                s = Symbol(ast[1])
            ctx.line += s
    elif ast[0] == token.DOT:
        if ctx.dotted_cnt > 0:
            # Add the "." to the include symbol, as we are
            # building a larger symbol from all the dotted names
            ctx.line += Symbol(ast[1], Mark.INCLUDE)
        else:
            # Add the "." as a non-symbol.
            ctx.line += NonSymbol(ast[1])
    elif token.ISEOF(ast[0]):
        # End of compilation: consume this token without adding it
        # to the line, committing any line being processed.
        ctx.commit()
    else:
        # All other tokens are simply added to the line
        ctx.line += NonSymbol(ast[1])

def processAst(ctx, ast):
    """ Process a given AST tuple
    """
    if token.ISNONTERMINAL(ast[0]):
        processNonTerminal(ctx, ast)
    else:
        processTerminal(ctx, ast)

def walkAst(ctx, ast):
    """ Scan the AST (tuple) for tokens, appending index lines to the buffer.
    """
    indent = 0
    stack = [(ast, indent)]
    while stack:
        ast, indent = stack.pop()

        #print "%s%s" % (" " * indent, nodeNames[ast[0]])
        processAst(ctx, ast)

        indented = False
        for i in range(len(ast)-1, 0, -1):
            if type(ast[i]) == types.TupleType:
                # Push it onto the processing stack
                # Mirrors a recursive solution
                if not indented:
                    indent += 2
                    indented = True
                stack.append((ast[i], indent))

def parseSource(sourcecode, indexbuff, indexbuff_len):
    """Parses python source code and puts the resulting index information into the buffer.
    """
    if len(sourcecode) == 0:
        return indexbuff_len

    # Parse the source to an Abstract Syntax Tree
    sourcecode = string.replace(sourcecode, '\r\n', '\n')
    if sourcecode[-1] != '\n':
        # We need to make sure files are terminated by a newline.
        sourcecode += '\n'
    ast = parser.suite(sourcecode)
    global debug
    if debug:
        dumpAst(ast)

    ctx = Context()

    walkAst(ctx, ast.totuple(True))
    indexbuff.extend(ctx.buff)
    indexbuff_len += len(ctx.buff)
    return indexbuff_len

def writeIndex(basepath, indexfn, indexbuff, fnamesbuff):
    """Write the index buffer to the output file.
    """
    fout = open(os.path.join(basepath, indexfn), 'w')

    # Write the header and index
    index = ''.join(indexbuff)
    index_len = len(index)
    hdr_len = len(basepath) + 25
    fout.write("cscope 15 %s -c %010d" % (basepath, hdr_len + index_len))
    fout.write(index)

    # Write trailer info
    fnames = '\n'.join(fnamesbuff) + '\n'
    fout.write("\n1\n.\n0\n")
    fout.write("%d\n" % len(fnamesbuff))
    fout.write("%d\n" % len(fnames))
    fout.write(fnames)
    fout.close()


if __name__ == "__main__":
    main()
