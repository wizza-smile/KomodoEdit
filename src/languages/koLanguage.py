# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1/GPL 2.0/LGPL 2.1
# 
# The contents of this file are subject to the Mozilla Public License
# Version 1.1 (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
# 
# Software distributed under the License is distributed on an "AS IS"
# basis, WITHOUT WARRANTY OF ANY KIND, either express or implied. See the
# License for the specific language governing rights and limitations
# under the License.
# 
# The Original Code is Komodo code.
# 
# The Initial Developer of the Original Code is ActiveState Software Inc.
# Portions created by ActiveState Software Inc are Copyright (C) 2000-2007
# ActiveState Software Inc. All Rights Reserved.
# 
# Contributor(s):
#   ActiveState Software Inc
# 
# Alternatively, the contents of this file may be used under the terms of
# either the GNU General Public License Version 2 or later (the "GPL"), or
# the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
# in which case the provisions of the GPL or the LGPL are applicable instead
# of those above. If you wish to allow use of your version of this file only
# under the terms of either the GPL or the LGPL, and not to allow others to
# use your version of this file under the terms of the MPL, indicate your
# decision by deleting the provisions above and replace them with the notice
# and other provisions required by the GPL or the LGPL. If you do not delete
# the provisions above, a recipient may use your version of this file under
# the terms of any one of the MPL, the GPL or the LGPL.
# 
# ***** END LICENSE BLOCK *****

# -*- python -*-
import sys
import os
from os.path import join, splitext
import glob
import fnmatch
import timeline
import re
import logging
from pprint import pprint, pformat

from xpcom import components, nsError, ServerException, COMException, _xpcom
import xpcom.server
from xpcom.server import WrapObject, UnwrapObject
from koTreeView import TreeView
import directoryServiceUtils

import koXMLTreeService

log = logging.getLogger('koLanguage')
#log.setLevel(logging.DEBUG)

def _cmpLen(a, b):
    al = len(a)
    bl = len(b)
    if al>bl: return -1
    if al==bl: return cmp(a, b)
    return 1

class KoLanguageItem:
    _com_interfaces_ = [components.interfaces.koIHierarchyItem]
    _reg_contractid_ = "@activestate.com/koLanguageItem;1"
    _reg_clsid_ = "{33532bc1-302a-4b2d-ad14-13c5eadf4d93}"
    
    def __init__(self, language, key):
        self.language = language
        self.name = language
        self.key = key
        
    def get_available_types(self):
        return components.interfaces.koIHierarchyItem.ITEM_STRING

    def get_item_string(self):
        return self.language

    def get_container(self):
        return 0

class KoLanguageContainer:
    _com_interfaces_ = [components.interfaces.koIHierarchyItem]
    _reg_contractid_ = "@activestate.com/koLanguageContainer;1"
    _reg_clsid_ = "{878ed885-6274-4c07-9668-a9a01a0ae09c}"
    
    def __init__(self, label, languages):
        self.name = label
        self.languages = languages
    
    def getChildren(self):
        return self.languages

    def get_available_types(self):
        return 0
        return components.interfaces.koIHierarchyItem.ITEM_STRING

    def get_item_string(self):
        return 0
        return self.label

    def get_container(self):
        return 1


# The LanguageRegistryService keeps track of which languages/services
# are available.  It is used by packages of services to inform the
# system of their presence, and is used internally by the other
# language services classes to get information on the services
# available.
class KoLanguageRegistryService:
    _com_interfaces_ = [components.interfaces.koILanguageRegistryService,
                        components.interfaces.nsIObserver]
    _reg_contractid_ = "@activestate.com/koLanguageRegistryService;1"
    _reg_clsid_ = "{4E76795E-CC92-47c6-8801-C9ACFC1B02E3}"

    # 'Primary' languages are those that the Komodo UI "cares" more about.
    # Mainly it means that they show up at the top-level in the "View As
    # Language" menulists.
    _primaryLanguageNames = {}    # use dict for lookup speed
    
    # 'Internal' languages are those that the user shouldn't see as a language
    # name choice directly. E.g. "Rx", "Regex".
    _internalLanguageNames = {}   # use dict for lookup speed
    
    _namespaceMap = {}
    _publicIdMap = {}
    _systemIdMap = {}
    
    # Language-specific patterns that will identify the language based on
    # a match against the head of the document.
    #
    # Note that some shebang "line" matches require more than the first
    # line so pattern should be prepared to deal with that.
    # XXX this belongs in language services
    shebangPatterns = []
    
    # Mapping of local variable mode names (typically Emacs mode name)
    # to the appropriate language name in this registry.
    # - All we need to note here are exceptions in the naming scheme,
    #   like "mode: C" which corresponds to Komodo's C++ language.
    _modeName2LanguageName = {}
    
    def __init__(self):
        timeline.enter('KoLanguageRegistryService.__init__')
        self.__initPrefs()
        self._globalPrefSvc = components.classes["@activestate.com/koPrefService;1"].\
                            getService(components.interfaces.koIPrefService)
        self._globalPrefs = self._globalPrefSvc.prefs

        self.__languageFromLanguageName = {} # e.g. "Python": <koILanguage 'Python'>
        
        # Accesskey (the Alt+<letter/number> keyboard shortcut to select a menu
        # item on Windows and Linux) for this language in menulists of language
        # names. Not every language has (or should have) one.
        self.__accessKeyFromLanguageName = {}
        
        # File association data. This data comes from a few places:
        # - the 'factoryFileAssociations' preference (the factory set
        #   of file associations)
        # - the 'fileAssociationDiffs' preference (site/user changes to the
        #   factory set)
        # - the 'defaultExtension' attribute of all registered languages
        #   (src/languages/ko*Language.py and any installed UDL-based
        #   language extensions) unless the given pattern is already used
        self.__languageNameFromPattern = {} # e.g. "*.py": "Python"
        # Used for creating user assocs diff.
        self.__factoryLanguageNameFromPattern = {}
        self.__patternsFromLanguageName = None  # e.g. "Python": ["*.py", "*.pyw"]
        # E.g. ".py": "Python", "Makefile": "Makefile", "Conscript": "Perl";
        # for faster lookup.
        self.__languageNameFromExtOrBasename = None

        # XXX get from prefs?
        self.defaultLanguage = "Text"
        
        self.registerLanguages()
        self._resetFileAssociationData() # must be after .registerLanguages()
        timeline.leave('KoLanguageRegistryService.__init__')

    def observe(self, aSubject, aTopic, someData):
        if someData == "fileAssociationDiffs":
            self.__languageNameFromPattern = None
            self.__languageNameFromExtOrBasename = None
            self.__patternsFromLanguageName = None
            self._resetFileAssociationData()

    def _resetFileAssociationData(self):
        timeline.enter('KoLanguageRegistryService._resetFileAssociationData')

        self.__languageNameFromPattern = {}
        self.__languageNameFromExtOrBasename = {}
        self.__factoryLanguageNameFromPattern = {}
        self.__patternsFromLanguageName = {}

        # Load 'factoryFileAssociations' pref.
        factoryFileAssociationsRepr \
            = self._globalPrefs.getStringPref("factoryFileAssociations")
        for pattern, languageName in eval(factoryFileAssociationsRepr).items():
            self._addOneFileAssociation(pattern, languageName)

        # Apply fallback default extensions from all registered languages.
        for languageName, language in self.__languageFromLanguageName.items():
            defaultExtension = language.defaultExtension
            if defaultExtension:
                if not defaultExtension.startswith('.'):
                    log.warn("'%s': skipping unexpected defaultExtension for "
                             "language '%s': it must begin with '.'",
                             defaultExtension, languageName)
                    continue

                self._addOneFileAssociation('*'+defaultExtension, languageName,
                                            override=False)

        # Make a copy of the current association set before applying
        # user/site-level changes so we can compare against it latter to know
        # what changes (if any) the user made.
        self.__factoryLanguageNameFromPattern \
            = self.__languageNameFromPattern.copy()

        # Load 'fileAssociationDiffs' pref.
        if self._globalPrefs.hasStringPref("fileAssociationDiffs"):
            fileAssociationDiffsRepr \
                = self._globalPrefs.getStringPref("fileAssociationDiffs")
            try:
                for action, pattern, languageName in eval(fileAssociationDiffsRepr):
                    if action == '+':
                        self._addOneFileAssociation(pattern, languageName)
                    elif action == '-':
                        self._removeOneFileAssociation(pattern, languageName)
                    else:
                        log.warn("unexpected action in 'fileAssociationDiffs' "
                                 "entry (skipping): %r",
                                 (action, pattern, languageName))
            except (SyntaxError, ValueError), ex:
                log.exception("error loading 'fileAssociationDiffs' "
                              "(skipping): %s", fileAssociationDiffsRepr)

        timeline.leave('KoLanguageRegistryService._resetFileAssociationData')

    def _removeOneFileAssociation(self, pattern, languageName):
        if languageName == self.__languageNameFromPattern.get(pattern):
            log.debug("remove '%s' -> '%s' file association", pattern,
                      languageName)
            del self.__languageNameFromPattern[pattern]
            self.__patternsFromLanguageName[languageName].remove(pattern)

            base, ext = splitext(pattern)
            if base == '*':  # i.e. pattern == "*.FOO"
                if languageName == self.__languageNameFromExtOrBasename.get(ext.lower()):
                    del self.__languageNameFromExtOrBasename[ext.lower()]
            elif '*' not in pattern:  # e.g. "Conscript", "Makefile"
                if languageName == self.__languageNameFromExtOrBasename.get(pattern.lower()):
                    del self.__languageNameFromExtOrBasename[pattern.lower()]

    def _addOneFileAssociation(self, pattern, languageName, override=True):
        """Add one file association to the internal data structures.

            "pattern" is the association pattern (e.g. "*.py")
            "languageName" is the language name (e.g. "Python")
            "override" is an optional boolean (default True) indicating
                whether this setting should override existing settings. This
                option is here so ko*Language.py components can specify a
                fallback "*.py" extension pattern for their filetypes, but
                the associations in the "fileAssociations" pref still wins.
        """
        if not override and pattern in self.__languageNameFromPattern:
            log.debug("KoLanguageRegistryService: not using default '%s' "
                      "pattern for '%s' language (already mapped to '%s')",
                      pattern, languageName,
                      self.__languageNameFromPattern[pattern])
            return
        #elif not override:
        #    print "using fallback defaultExtension: '%s' -> '%s'" \
        #          % (pattern, languageName)

        self.__languageNameFromPattern[pattern] = languageName

        if languageName not in self.__patternsFromLanguageName:
            self.__patternsFromLanguageName[languageName] = []
        self.__patternsFromLanguageName[languageName].append(pattern)

        base, ext = splitext(pattern)
        if base == '*':  # i.e. pattern == "*.FOO"
            self.__languageNameFromExtOrBasename[ext.lower()] = languageName
        elif '*' not in pattern:  # e.g. "Conscript", "Makefile"
            self.__languageNameFromExtOrBasename[pattern.lower()] = languageName

    def getLanguageHierarchy(self):
        """Return the structure used to define the language name menulist
        used in various places in the Komodo UI.
        """
        primaries = []
        others = []
        for languageName in self.__languageFromLanguageName:
            if languageName in self._internalLanguageNames:
                continue
            elif languageName in self._primaryLanguageNames:
                primaries.append(languageName)
            else:
                others.append(languageName)
        primaries.sort()
        others.sort()

        otherContainer = KoLanguageContainer('Other',
            [KoLanguageItem(ln, self.__accessKeyFromLanguageName[ln])
             for ln in others])
        primaryContainer = KoLanguageContainer('',
            [KoLanguageItem(ln, self.__accessKeyFromLanguageName[ln])
             for ln in primaries]
            + [otherContainer])
        return primaryContainer

    def changeLanguageStatus(self, languageName, status):
        lang = self.getLanguage(languageName)
        lang.primary = status
        if status:
            self._primaryLanguageNames[languageName] = True
        else:
            try:
                del self._primaryLanguageNames[languageName]
            except KeyError:
                pass

    def getLanguageNames(self):
        languageNames = self.__languageFromLanguageName.keys()
        languageNames.sort()
        languageNames = [name for name in languageNames
                         if name not in self._internalLanguageNames]
        return languageNames
    
    def patternsFromLanguageName(self, languageName):
        if self.__patternsFromLanguageName is None:
            self._resetFileAssociationData()
        return self.__patternsFromLanguageName.get(languageName, [])

    def registerLanguages(self):
        """registerLanguages
        
        Gets a list of files from the same directory this file
        is located in, and calls the register function.
        """
        componentDirs = directoryServiceUtils.getComponentsDirectories()
        self._languageSpecificPrefs = self._globalPrefSvc.prefs.getPref("languages")
        for dirname in componentDirs:
            log.info("looking for languages in [%s]", dirname)
            files = glob.glob(join(dirname, 'ko*Language.py'))
            for file in files:
                # REFACTOR: Should use imp.load_module() instead of execfile
                #           here. And use better error message.
                try:
                    module = {}
                    execfile(file, module)
                    if 'registerLanguage' in module:
                        module['registerLanguage'](self)
                except Exception, e:
                    log.exception(e)            

    def registerLanguage(self, language):
        name = language.name
        assert not self.__languageFromLanguageName.has_key(name), \
               "Language '%s' already registered" % (name)
        log.info("registering language [%s]", name)
        
        self.__languageFromLanguageName[name] = language
        language = UnwrapObject(language)
        self.__accessKeyFromLanguageName[name] = language.accessKey

        # Update fields based on user preferences:
        languageKey = "languages/" + language.name
        if self._languageSpecificPrefs.hasPref(languageKey):
            languagePrefs = self._languageSpecificPrefs.getPref(languageKey)
            if languagePrefs.hasPref("primary"):
                language.primary = languagePrefs.getBooleanPref("primary")

        # So that we can tell that, for example:
        #     -*- mode: javascript -*-
        # means language name "JavaScript".
        if language.modeNames:
            for modeName in language.modeNames:
                self._modeName2LanguageName[modeName.lower()] = name
        else:
            self._modeName2LanguageName[name.lower()] = name
        if language.primary:
            self._primaryLanguageNames[name] = True
        if language.internal:
            self._internalLanguageNames[name] = True
        for pat in language.shebangPatterns:
            self.shebangPatterns.append((name, pat))
        for ns in language.namespaces:
            self._namespaceMap[ns] = name
        for id in language.publicIdList:
            self._publicIdMap[id] = name
        for id in language.systemIdList:
            self._systemIdMap[id] = name


    def getLanguage(self, language):
        # return a koILanguage for language.  Create it if it does not
        # exist yet.
        if not language: language=self.defaultLanguage
        
        if language not in self.__languageFromLanguageName:
            log.warn("Asked for unknown language: %r", language)
            if language != self.defaultLanguage:
                return self.getLanguage(self.defaultLanguage)
            # Trouble if we can't load the default language.
            return None

        if self.__languageFromLanguageName[language] is None:
            contractid = "@activestate.com/koLanguage?language=%s;1" \
                         % (language)
            self.__languageFromLanguageName[language] = components.classes[contractid] \
                    .createInstance(components.interfaces.koILanguage)

        return self.__languageFromLanguageName[language]

    def suggestLanguageForFile(self, basename):
        if self.__languageNameFromPattern is None:
            self._resetFileAssociationData()

        # First try to look up the language name from the file extension or
        # plain basename: faster.  We use the longest possible extension so
        # we can match things like *.django.html
        if basename.find('.') > 0:
            base, ext = basename.split('.', 1)
        else:
            base = basename
            ext = None
        if ext and ext.lower() in self.__languageNameFromExtOrBasename:
            #print "debug: suggestLanguageForFile: '%s' -> '%s'" \
            #      % (ext, self.__languageNameFromExtOrBasename[ext.lower()])
            return self.__languageNameFromExtOrBasename[ext.lower()]
        elif basename.lower() in self.__languageNameFromExtOrBasename:
            #print "debug: suggestLanguageForFile: '%s' -> '%s'" \
            #      % (basename,
            #         self.__languageNameFromExtOrBasename[basename.lower()])
            return self.__languageNameFromExtOrBasename[basename.lower()]

        # Next, try each registered filename glob pattern: slower.  Use the
        # longest pattern first
        patterns = self.__languageNameFromPattern.keys()
        if patterns:
            patterns.sort(_cmpLen)
            for pattern in patterns:
                if fnmatch.fnmatch(basename, pattern):
                    #print "debug: suggestLanguageForFile: '%s' -> '%s'" \
                    #      % (pattern, self.__languageNameFromPattern[pattern])
                    return self.__languageNameFromPattern[pattern]

        return ''  # indicates that we don't know the lang name

    def getFileAssociations(self):
        """Return the list of the file associations:
            <pattern> -> <language name>
        
        - They are returned as two separate lists for simplicity of passing
          across XPCOM.
        - The list is sorted.
        """
        associations = [(p, ln) for (p, ln) in self.__languageNameFromPattern.items()]
        associations.sort()
        return ([p  for p,ln in associations],
                [ln for p,ln in associations])

    def createFileAssociationPrefString(self, patterns, languageNames):
        """Create a pref string from the given set of file associations.
        
        Typically called by the "File Associations" preferences panel.
        Instead of saving the full associations list in the user's prefs, we
        just save a diff against the "factory" associations list.
        """
        #TODO: Warn/die if any duplicate patterns: indicates bug in caller.

        # Massage data for faster lookup.
        #       {'a': 1, 'b': 2}   ->   {('a', 1): True, ('b', 2): True}
        factoryAssociations = dict(
            ((k,v), True) for k,v in self.__factoryLanguageNameFromPattern.items()
        )
        associations = dict(
            ((k,v), True) for k,v in zip(patterns, languageNames)
        )

        # Calculate the diffs. ('p' == pattern, 'ln' == language name)
        additions = [('+', p, ln) for (p, ln) in associations.keys()
                                   if (p, ln) not in factoryAssociations]
        deletions = [('-', p, ln) for (p, ln) in factoryAssociations.keys()
                                   if (p, ln) not in associations]
        diffs = additions + deletions
        return repr(diffs)

    def saveFileAssociations(self, patterns, languageNames):
        """Save the given set of file associations."""
        assocPref = self.createFileAssociationPrefString(patterns, languageNames)
        self._globalPrefs.setStringPref("fileAssociationDiffs", assocPref)

    def _sendStatusMessage(self, msg, timeout=3000, highlight=1):
        observerSvc = components.classes["@mozilla.org/observer-service;1"]\
                      .getService(components.interfaces.nsIObserverService)
        sm = components.classes["@activestate.com/koStatusMessage;1"]\
             .createInstance(components.interfaces.koIStatusMessage)
        sm.category = "language_registry"
        sm.msg = msg
        sm.timeout = timeout     # 0 for no timeout, else a number of milliseconds
        sm.highlight = highlight # boolean, whether or not to highlight
        try:
            observerSvc.notifyObservers(sm, "status_message", None)
        except COMException, e:
            # do nothing: Notify sometimes raises an exception if (???)
            # receivers are not registered?
            pass

    emacsLocalVars1_re = re.compile("-\*-\s*(.*?)\s*-\*-")
    # This regular expression is intended to match blocks like this:
    #    PREFIX Local Variables: SUFFIX
    #    PREFIX mode: Tcl SUFFIX
    #    PREFIX End: SUFFIX
    # Some notes:
    # - "[ \t]" is used instead of "\s" to specifically exclude newlines
    # - "(\r\n|\n|\r)" is used instead of "$" because the sre engine does
    #   not like anything other than Unix-style line terminators.
    emacsLocalVars2_re = re.compile(r"^(?P<prefix>(?:[^\r\n|\n|\r])*?)[ \t]*Local Variables:[ \t]*(?P<suffix>.*?)(?:\r\n|\n|\r)(?P<content>.*?\1End:)",
                                    re.IGNORECASE | re.MULTILINE | re.DOTALL)
    def _getEmacsLocalVariables(self, head, tail):
        """Return a dictionary of emacs local variables.
        
            "head" is a sufficient amount of text from the start of the file.
            "tail" is a sufficient amount of text from the end of the file.
        
        Parsing is done according to this spec (and according to some
        in-practice deviations from this):
            http://www.gnu.org/software/emacs/manual/html_chapter/emacs_33.html#SEC485
        Note: This has moved to:
            http://www.gnu.org/software/emacs/manual/emacs.html#File-Variables
            
        A ValueError is raised is there is a problem parsing the local
        variables.
        """
        localVars = {}

        # Search the head for a '-*-'-style one-liner of variables.
        if head.find("-*-") != -1:
            match = self.emacsLocalVars1_re.search(head)
            if match:
                localVarsStr = match.group(1)
                if '\n' in localVarsStr:
                    raise ValueError("local variables error: -*- not "
                                     "terminated before end of line")
                localVarStrs = [s.strip() for s in localVarsStr.split(';') if s.strip()]
                if len(localVarStrs) == 1 and ':' not in localVarStrs[0]:
                    # While not in the spec, this form is allowed by emacs:
                    #   -*- Tcl -*-
                    # where the implied "variable" is "mode". This form is only
                    # allowed if there are no other variables.
                    localVars["mode"] = localVarStrs[0].strip()
                else:
                    for localVarStr in localVarStrs:
                        try:
                            variable, value = localVarStr.strip().split(':', 1)
                        except ValueError:
                            raise ValueError("local variables error: malformed -*- line")
                        # Lowercase the variable name because Emacs allows "Mode"
                        # or "mode" or "MoDe", etc.
                        localVars[variable.lower()] = value.strip()

        # Search the tail for a "Local Variables" block.
        match = self.emacsLocalVars2_re.search(tail)
        if match:
            prefix = match.group("prefix")
            suffix = match.group("suffix")
            lines = match.group("content").splitlines(0)
            #print "prefix=%r, suffix=%r, content=%r, lines: %s"\
            #      % (prefix, suffix, match.group("content"), lines)
            # Validate the Local Variables block: proper prefix and suffix
            # usage.
            for i in range(len(lines)):
                line = lines[i]
                if not line.startswith(prefix):
                    raise ValueError("local variables error: line '%s' "
                                     "does not use proper prefix '%s'"
                                     % (line, prefix))
                # Don't validate suffix on last line. Emacs doesn't care,
                # neither should Komodo.
                if i != len(lines)-1 and not line.endswith(suffix):
                    raise ValueError("local variables error: line '%s' "
                                     "does not use proper suffix '%s'"
                                     % (line, suffix))
            # Parse out one local var per line.
            for line in lines[:-1]: # no var on the last line ("PREFIX End:")
                if prefix: line = line[len(prefix):] # strip prefix
                if suffix: line = line[:-len(suffix)] # strip suffix
                line = line.strip()
                try:
                    variable, value = line.split(':', 1)
                except ValueError:
                    raise ValueError("local variables error: missing colon "
                                     "in local variables entry: '%s'" % line)
                # Do NOT lowercase the variable name, because Emacs only
                # allows "mode" (and not "Mode", "MoDe", etc.) in this block.
                localVars[variable] = value.strip()

        return localVars

    htmldoctype_re = re.compile('<!DOCTYPE\s+html',
                                re.IGNORECASE)
    def guessLanguageFromContents(self, head, tail):
        """Guess the language (e.g. Perl, Tcl) of a file from its contents.
        
            "head" is a sufficient amount of text from the start of the file
                where "sufficient" is undefined (although, realistically
                at least the first few lines should be passed in to get good
                coverage).
            "tail" is a sufficient amount of text from the end of the file,
                where "sufficient" is as above. (Usually the tail of the
                document is where Emacs-style local variables. Emacs'
                documentation says this block should be "near the end of
                the file, in the last page.")

        This method returns a list of possible languages with the more
        likely, or more specific, first. If no information can be gleaned an
        empty list is returned.
        """
        langs = []

        # Specification of the language via Emacs-style local variables
        # wins, so we check for it first.
        #   http://www.gnu.org/manual/emacs-21.2/html_mono/emacs.html#SEC486
        if self._globalPrefs.getBooleanPref("emacsLocalModeVariableDetection"):
            # First check for one-line local variables.
            # - The Emacs spec says this has to be in the _first_ line,
            #   but in practice this seems to be "near the top".
            try:
                localVars = self._getEmacsLocalVariables(head, tail)
            except ValueError, ex:
                self._sendStatusMessage(str(ex))
            else:
                if localVars.has_key("mode"):
                    mode = localVars["mode"]
                    try:
                        langName = self._modeName2LanguageName[mode.lower()]
                    except KeyError:
                        log.warn("unknown emacs mode: '%s'", mode)
                    else:
                        langs = [langName]

        # Detect if this is an XML file.
        if self._globalPrefs.getBooleanPref('xmlDeclDetection') and \
            (not langs or 'XML' in langs):
            # it may be an XHTML file
            lhead = head.lower()
            if lhead.startswith(u'<?xml'):
                langs.append("XML")

            try:
                # find the primary namespace of the first node
                tree = koXMLTreeService.getService().getTreeForContent(head)
                if tree.root is not None:
                    ns = tree.namespace(tree.root)
                    #print "XML NS [%s]" % ns
                    if ns in self._namespaceMap:
                        #print "language is [%s]" % self._namespaceMap[ns]
                        langs.append(self._namespaceMap[ns])
    
                # use the doctype decl if one exists
                if tree.doctype:
                    #print "XML doctype [%s]" % repr(tree.doctype)
                    if tree.doctype[2] in self._publicIdMap:
                        langs.append(self._publicIdMap[tree.doctype[2]])
                    if tree.doctype[3] in self._systemIdMap:
                        langs.append(self._systemIdMap[tree.doctype[3]])
                    if tree.doctype[0].lower() == "html":
                        langs.append("HTML")
                elif "<!doctype html>" in lhead:
                    langs.append("HTML5")
            except Exception, e:
                # log this, but keep on going, it's just a failure in xml
                # parsing and we can live without it.  bug 67251
                log.exception(e)
            langs.reverse()
            #print "languages are %r"%langs

        # Detect the type from a possible shebang line.
        if (self._globalPrefs.getBooleanPref('shebangDetection') and
            not langs and head.startswith(u'#!')):
            shebangLangs = []
            for language, pattern in self.shebangPatterns:
                if pattern.match(head):
                    shebangLangs.append(language)
            if len(shebangLangs) > 1:
                self._sendStatusMessage("language determination error: "
                    "ambiguous shebang (#!) line: indicates all of '%s'"
                    % "', '".join(shebangLangs))
            else:
                langs = shebangLangs

        return langs
        
    def __initPrefs(self):
        self.__prefs = components \
                       .classes["@activestate.com/koPrefService;1"] \
                       .getService(components.interfaces.koIPrefService)\
                       .prefs
        # Observers will be QI'd for a weak-reference, so we must keep the
        # observer alive ourself, and must keep the COM object alive,
        # _not_ just the Python instance!!!
        # XXX - this is a BUG in the weak-reference support.
        # It should NOT be necessary to do this, as the COM object is
        # kept alive by the service manager.  I suspect that this bug
        # happens due to the weak-reference being made during
        # __init__.  FIXME!
        self._observer = xpcom.server.WrapObject(self,
                                      components.interfaces.nsIObserver)
        self.__prefs.prefObserverService.addObserver(self._observer,
                                                     'fileAssociationDiffs',
                                                     True)


# Used for the Primary Languages tree widget in 
# pref-languages.xul/js.
# Based on KoCodeIntelCatalogsTreeView
class KoLanguageStatusTreeView(TreeView):
    _com_interfaces_ = [components.interfaces.koILanguageStatusTreeView,
                        components.interfaces.nsITreeView]
    _reg_clsid_ = "{6e0068df-0b51-47ae-9195-8309b52eb78c}"
    _reg_contractid_ = "@activestate.com/koLanguageStatusTreeView;1"
    _reg_desc_ = "Komodo Language Status list tree view"
    _col_id = "languageStatus-status"
    _prefix = "languageStatus-"

    def __init__(self):
        TreeView.__init__(self) # for debug logging: , debug="languageStatus")
        self._rows = []
        # Atoms for styling the checkboxes.
        atomSvc = components.classes["@mozilla.org/atom-service;1"].\
                  getService(components.interfaces.nsIAtomService)
        self._sortColAtom = atomSvc.getAtom("sort-column")
        self._filter = self._filter_lc = ""
        
    def init(self):
        self._sortData = (None, None)
        self._loadAllLanguages()
        self._reload()
        self._wasChanged = False

    def _loadAllLanguages(self):
        self._allRows = []
        langRegistry = components.classes["@activestate.com/koLanguageRegistryService;1"].getService(components.interfaces.koILanguageRegistryService)
        langNames = langRegistry.getLanguageNames()
        for langName in langNames:
            lang = UnwrapObject(langRegistry.getLanguage(langName))
            if not lang.internal:
                self._allRows.append({'name':langName,
                                      'name_lc':langName.lower(),
                                      'status':lang.primary,
                                      'origStatus':lang.primary})

    def _reload(self):
        oldRowCount = len(self._rows)
        self._rows = []
        for row in self._allRows:
            if not self._filter or self._filter_lc in row['name_lc']:
                self._rows.append(row)
        if self._sortData == (None, None):
            self._rows.sort(key=lambda r: (r['name_lc']))
        else:
            # Allow for sorting by both name and status
            sort_key, sort_is_reversed = self._sortData
            self._do_sort(sort_key, sort_is_reversed)

        if self._tree:
            self._tree.beginUpdateBatch()
            newRowCount = len(self._rows)
            self._tree.rowCountChanged(oldRowCount, newRowCount - oldRowCount)
            self._tree.invalidate()
            self._tree.endUpdateBatch()

    def save(self):
        if not self._wasChanged:
            return
        langRegistry = UnwrapObject(components.classes["@activestate.com/koLanguageRegistryService;1"].getService(components.interfaces.koILanguageRegistryService))
        languageSpecificPrefs = components.classes["@activestate.com/koPrefService;1"].\
                            getService(components.interfaces.koIPrefService).\
                            prefs.getPref("languages")
        for row in self._rows:
            langName, status, origStatus = row['name'], row['status'], row['origStatus']
            if status != origStatus:
                langRegistry.changeLanguageStatus(langName, status)
                # Update the pref
                languageKey = "languages/" + langName
                if languageSpecificPrefs.hasPref(languageKey):
                    languageSpecificPrefs.getPref(languageKey).setBooleanPref("primary", bool(status))
                else:
                    prefSet = components.classes["@activestate.com/koPreferenceSet;1"].\
                        createInstance(components.interfaces.koIPreferenceSet)
                    prefSet.setBooleanPref("primary", bool(status))
                    languageSpecificPrefs.setPref(languageKey, prefSet)
        obsSvc = components.classes["@mozilla.org/observer-service;1"].\
                 getService(components.interfaces.nsIObserverService)
        obsSvcProxy = _xpcom.getProxyForObject(None,
            components.interfaces.nsIObserverService, obsSvc,
            _xpcom.PROXY_ALWAYS | _xpcom.PROXY_SYNC)
        obsSvcProxy.notifyObservers(None, 'primary_languages_changed', '')

    def toggleStatus(self, row_idx):
        """Toggle selected state for the given row."""
        self._rows[row_idx]["status"] = not self._rows[row_idx]["status"]
        self._wasChanged = True
        if self._tree:
            self._tree.invalidateRow(row_idx)
        

    def set_filter(self, filter):
        self._filter = filter
        self._filter_lc = self._filter.lower()
        self._reload()

    def get_filter(self):
        return self._filter

    def get_sortColId(self):
        sort_key = self._sortData[0]
        if sort_key is None:
            return None
        else:
            return "languageStatus-" + sort_key
        
    def get_sortDirection(self):
        return self._sortData[1] and "descending" or "ascending"
        
    def get_rowCount(self):
        return len(self._rows)

    def getCellValue(self, row_idx, col):
        assert col.id == self._col_id
        return self._rows[row_idx]["status"] and "true" or "false"
    

    def setCellValue(self, row_idx, col, value):
        assert col.id == self._col_id
        self._wasChanged = True
        self._rows[row_idx]["status"] = (value == "true" and True or False)
        if self._tree:
            self._tree.invalidateRow(row_idx)

    def getCellText(self, row_idx, col):
        if col.id == self._col_id:
            return ""
        else:
            try:
                key = col.id[len("languageStatus-"):]
                return self._rows[row_idx][key]
            except KeyError, ex:
                raise ValueError("getCellText: unexpected col.id: %r" % col.id)

    def isEditable(self, row_idx, col):
        if col.id == self._col_id:
            return True
        else:
            return False

    def getColumnProperties(self, col, properties):
        if col.id[len("languageStatus-"):] == self._sortData[0]:
            properties.AppendElement(self._sortColAtom)

    def isSorted(self):
        return self._sortData != (None, None)

    def cycleHeader(self, col):
        sort_key = col.id[len("languageStatus-"):]
        old_sort_key, old_sort_is_reversed = self._sortData
        if sort_key == old_sort_key:
            sort_is_reversed = not old_sort_is_reversed
            self._rows.reverse()
        else:
            sort_is_reversed = False
            self._do_sort(sort_key, sort_is_reversed)
        self._sortData = (sort_key, sort_is_reversed)
        if self._tree:
            self._tree.invalidate()

    def _do_sort(self, sort_key, sort_is_reversed):
        if sort_key == 'status':
            self._rows.sort(key=lambda r: (not r['status'],
                                           r['name_lc']),
                            reverse=sort_is_reversed)
        else:
            self._rows.sort(key=lambda r: r['name_lc'],
                            reverse=sort_is_reversed)




#---- self-test suite for the guessLanguageFromContents implementation

if __name__ == "__main__":
    import unittest
    from xpcom.server import UnwrapObject

    regSvc = components.classes['@activestate.com/koLanguageRegistryService;1'].\
             getService(components.interfaces.koILanguageRegistryService)
    regSvc = UnwrapObject(regSvc)
    
    class GuessLangTestCase(unittest.TestCase):
        def _assertPossibleLanguagesAre(self, head, tail, expected):
            actual = regSvc.guessLanguageFromContents(head, tail)
            if actual != expected:
                errmsg = """guessed possible languages do not match expected results:
Expected: %s
Got:      %s
---------------- head of document --------------------------------
%s
---------------- tail of document --------------------------------
%s
------------------------------------------------------------------
""" % (expected, actual, head, tail)
                self.fail(errmsg)

        def _assertEmacsLocalVarsAre(self, head, tail, expected):
            actual = regSvc._getEmacsLocalVariables(head, tail)
            if actual != expected:
                errmsg = """Emacs-style local variable results are not as expected:
Expected: %s
Got:      %s
---------------- head of document --------------------------------
%s
---------------- tail of document --------------------------------
%s
------------------------------------------------------------------
""" % (expected, actual, head, tail)
                self.fail(errmsg)

        def test_perl_shebang(self):
            perlHeads = [
                "#!perl",
                "#!perl5 -w",
                "#!/bin/perl",
                "#! /bin/perl -dwh  ",
                "#!/bin/PeRl",
                "#!/bin/myperl",
            ]
            for head in perlHeads:
                self._assertPossibleLanguagesAre(head, "", ["Perl"])
            notPerlHeads = [
                "foo",
                "#!/bin/erl",
            ]
            for head in notPerlHeads:
                self._assertPossibleLanguagesAre(head, "", [])
        def test_python_shebang(self):
            pythonHeads = [
                "#!python",
                "#!python22 -w",
                "#!/bin/python",
                "#! /bin/python -dwh  ",
                "#!/bin/PyThOn",
                "#!/bin/mypython",
            ]
            for head in pythonHeads:
                self._assertPossibleLanguagesAre(head, "", ["Python"])
            notPythonHeads = [
                "foo",
                "#!/bin/ython",
            ]
            for head in notPythonHeads:
                self._assertPossibleLanguagesAre(head, "", [])
        def test_tcl_shebang(self):
            tclHeads = [
                "#!tclsh",
                "#!tclsh82",
                "#!/bin/tclsh",
                "#!/bin/expect",
                "#! /bin/wish -v  ",
                "#!/bin/TcLSh",
                "#!/bin/mytclsh",
                """\
#!/bin/sh
# the next line restarts using tclsh \\
exec tclsh "$0" "$@"
""",
                """\
#!/bin/sh
# the next line restarts using tclsh \\
exec wish "$0" "$@"
""",
                """\
#!/bin/sh
# the next line restarts using tclsh \\
exec expect "$0" "$@"
""",
            ]
            for head in tclHeads:
                self._assertPossibleLanguagesAre(head, "", ["Tcl"])
            notTclHeads = [
                "foo",
                "#!/bin/clsh",
            ]
            for head in notTclHeads:
                self._assertPossibleLanguagesAre(head, "", [])

        def test_emacs_local_variables(self):
            # Ensure the pref to use emacs-style local variables is on.
            globalPrefs = components.classes["@activestate.com/koPrefService;1"].\
                          getService(components.interfaces.koIPrefService).prefs
            emacsLocalModeVariableDetection = globalPrefs.getBooleanPref("emacsLocalModeVariableDetection")
            globalPrefs.setBooleanPref("emacsLocalModeVariableDetection", 1)

            headsAndTailsAndVarsAndLangs = [
                ("# -*- mode: Tcl -*-", "", {"mode": "Tcl"}, ["Tcl"]),
                ("# -*- Mode: Tcl -*-", "", {"mode": "Tcl"}, ["Tcl"]),
                ("# -*- mode: tcl -*-", "", {"mode": "tcl"}, ["Tcl"]),
                ("# *- mode: Tcl -*- blah blah", "", {}, []),
                ("", """\
# Using a prefix and suffix.
PREFIX Local Variables: SUFFIX
PREFIX mode: Tcl SUFFIX
PREFIX End: SUFFIX
""", {"mode": "Tcl"}, ["Tcl"]),
                ("", """\
Local Variables:
mode: Tcl
End:
""", {"mode": "Tcl"}, ["Tcl"]),
                ("", """\
# Using a realistic prefix.
# Local Variables:
# mode: Tcl
# End:
""", {"mode": "Tcl"}, ["Tcl"]),
                ("", """\
# Make sure the "End:" in a variable value does not screw up parsing.
PREFIX Local variables: SUFFIX
PREFIX foo: End: SUFFIX
PREFIX tab-width: 4 SUFFIX
PREFIX End: SUFFIX
""", {"foo": "End:", "tab-width": "4"}, []),
                ("", """\
# Must use proper prefix.
PREFIX Local Variables: SUFFIX
 mode: Tcl SUFFIX
PREFIX End: SUFFIX
""", ValueError, []),
                ("", """\
# Must use proper suffix.
PREFIX Local Variables: SUFFIX
PREFIX mode: Tcl
PREFIX End: SUFFIX
""", ValueError, []),
                ("", """\
# Whitespace after the prefix and before the suffix is allowed.
# The suffix on the "End:" line is not checked.
PREFIX Local Variables: SUFFIX
PREFIX    mode: Tcl	SUFFIX
PREFIX End:
""", {"mode": "Tcl"}, ["Tcl"]),
                ("", """\
# In the Local Variables block variable names are NOT lowercase'd, therefore
# the user must specify the lowercase "mode" to properly set the mode.
PREFIX Local Variables: SUFFIX
PREFIX MoDe: tcl SUFFIX
PREFIX End: SUFFIX
""", {"MoDe": "tcl"}, []),
                ("# -*- Mode: perl -*-", """\
# The local variables block beats the one-liner.
Local Variables:
mode: Tcl
End:
""", {"mode": "Tcl"}, ["Tcl"]),
            ]
            for head, tail, vars, langs in headsAndTailsAndVarsAndLangs:
                if isinstance(vars, dict):
                    self._assertEmacsLocalVarsAre(head, tail, vars)
                elif issubclass(vars, Exception):
                    self.assertRaises(ValueError, regSvc._getEmacsLocalVariables,
                                      head, tail)
                else:
                    raise "Unexpected test case 'vars' type: %s" % type(vars)
                self._assertPossibleLanguagesAre(head, tail, langs)

            # Restore the pref.
            globalPrefs.setBooleanPref("emacsLocalModeVariableDetection",
                                       emacsLocalModeVariableDetection)


        def test_bug28775(self):
            # The problem here was that no path info between the "exec"
            # and the "wish" was allowed.
            head = """\
#!/bin/sh
# aem \\
exec $AUTOTEST/bin/wish "$0" ${1+"$@"} &
"""
            self._assertPossibleLanguagesAre(head, "", ["Tcl"])

            # The problem here was that if there was a blank line with an
            # LF line terminator (i.e. Unix EOLs) before the "Local Variables"
            # line, then the LF would get included in the "prefix" and
            # subsequent lines would look like they didn't have the proper
            # prefix.
            tail = """
# ;;; Local Variables: ***
# ;;; mode: tcl ***
# ;;; End: ***
"""
            self._assertEmacsLocalVarsAre("", tail, {"mode": "tcl"})


    sys.argv.append('-v') # hackily make the test suite run in verbose mode
    unittest.main()



# Local Variables:
# mode: Python
# End:
