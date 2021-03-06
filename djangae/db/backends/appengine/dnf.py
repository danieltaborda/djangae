import copy
from itertools import  product
from django.db.models.sql.datastructures import EmptyResultSet
from djangae.db.backends.appengine.query import WhereNode
from django.db import NotSupportedError

def preprocess_node(node, negated):

    to_remove = []

    # Go through the children of this node and if any of the
    # child nodes are leaf nodes, then explode them if necessary
    for child in node.children:
        if child.is_leaf:
            if child.operator == "ISNULL":
                value = not child.value if node.negated else child.value
                if value:
                    child.operator = "="
                    child.value = None
                else:
                    child.operator = ">"
                    child.value = None

            elif node.negated and child.operator == "=":
                # Excluded equalities become inequalities

                lhs, rhs = WhereNode(), WhereNode()
                lhs.column = rhs.column = child.column
                lhs.value = rhs.value = child.value
                lhs.operator = "<"
                rhs.operator = ">"

                child.operator = child.value = child.column = None
                child.connector = "OR"
                child.children = [lhs, rhs]

                assert not child.is_leaf

            elif child.operator == "IN":
                # Explode IN filters into a series of 'OR statements to make life
                # easier later
                new_children = []

                for value in child.value:
                    if node.negated:
                        lhs, rhs = WhereNode(), WhereNode()
                        lhs.column = rhs.column = child.column
                        lhs.value = rhs.value = value
                        lhs.operator = "<"
                        rhs.operator = ">"

                        bridge = WhereNode()
                        bridge.connector = "OR"
                        bridge.children = [lhs, rhs]

                        new_children.append(bridge)
                    else:
                        new_node = WhereNode()
                        new_node.operator = "="
                        new_node.value = value
                        new_node.column = child.column
                        new_children.append(new_node)

                child.column = None
                child.operator = None
                child.connector = "AND" if negated else "OR"
                child.value = None
                child.children = new_children

                assert not child.is_leaf

            elif child.operator == "RANGE":
                lhs, rhs = WhereNode(), WhereNode()
                lhs.column = rhs.column = child.column
                if node.negated:
                    lhs.operator = "<"
                    rhs.operator = ">"
                    child.connector = "OR"
                else:
                    lhs.operator = ">="
                    rhs.operator = "<="
                    child.connector = "AND"
                lhs.value = child.value[0]
                rhs.value = child.value[1]

                child.column = child.operator = child.value = None
                child.children = [lhs, rhs]

                assert not child.is_leaf
        elif node.negated:
            # Move the negation down the tree
            child.negated = not child.negated

    # If this node was negated, we flip everything
    if node.negated:
        node.negated = False
        node.connector = "AND" if node.connector == "OR" else "OR"

    for child in to_remove:
        node.children.remove(child)

    return node


def normalize_query(query):
    where = query.where

    # If there are no filters then this is already normalized
    if where is None:
        return query

    def walk_tree(where, original_negated=False):
        negated = original_negated

        if where.negated:
            negated = not negated

        preprocess_node(where, negated)

        rewalk = False
        for child in where.children:
            if where.connector == "AND" and child.children and child.connector == 'AND' and not child.negated:
                where.children.remove(child)
                where.children.extend(child.children)
                rewalk = True
            elif child.connector == "AND" and len(child.children) == 1 and not child.negated:
                # Promote leaf nodes if they are the only child under an AND. Just for consistency
                where.children.remove(child)
                where.children.extend(child.children)
                rewalk = True
            elif len(child.children) > 1 and child.connector == 'AND' and child.negated:
                new_grandchildren = []
                for grandchild in child.children:
                    new_node = WhereNode()
                    new_node.negated = True
                    new_node.children = [grandchild]
                    new_grandchildren.append(new_node)
                child.children = new_grandchildren
                child.connector = 'OR'
                rewalk = True
            else:
                walk_tree(child, negated)

        if rewalk:
            walk_tree(where, original_negated)

        if where.connector == 'AND' and any([x.connector == 'OR' for x in where.children]):
            # ANDs should have been taken care of!
            assert not any([x.connector == 'AND' and not x.is_leaf for x in where.children ])

            product_list = []
            for child in where.children:
                if child.connector == 'OR':
                    product_list.append(child.children)
                else:
                    product_list.append([child])

            producted = product(*product_list)

            new_children = []
            for branch in producted:
                new_and = WhereNode()
                new_and.connector = 'AND'
                new_and.children = list(copy.deepcopy(branch))
                new_children.append(new_and)

            where.connector = 'OR'
            where.children = list(set(new_children))
            walk_tree(where, original_negated)

        elif where.connector == 'OR':
            new_children = []
            for child in where.children:
                if child.connector == 'OR':
                    new_children.extend(child.children)
                else:
                    new_children.append(child)
            where.children = list(set(new_children))

    walk_tree(where)

    if where.connector != 'OR':
        new_node = WhereNode()
        new_node.connector = 'OR'
        new_node.children = [where]
        query._where = new_node

    all_pks = True
    for and_branch in query.where.children:
        if and_branch.is_leaf:
            children = [and_branch]
        else:
            children = and_branch.children

        for node in children:
            if node.column == "__key__" and node.operator in ("=", "IN"):
                break
        else:
            all_pks = False
            break

    if (not all_pks) and len(query.where.children) > 30:
        raise NotSupportedError("Unable to run query as it required more than 30 subqueries")

    def remove_empty_in(node):
        """
            Once we are normalized, if any of the branches filters
            on an empty list, we can remove that entire branch from the
            query. If this leaves no branches, then the result set is empty
        """

        # This is a bit ugly, but you try and do it more succinctly :)
        # We have the following possible situations for IN queries with an empty
        # value:

        # - Negated: One of the nodes in the and branch will always be true and is therefore
        #    unnecessary, we leave it alone though
        # - Not negated: The entire AND branch will always be false, so that branch can be removed
        #    if that was the last branch, then the queryset will be empty

        # Everything got wiped out!
        if node.connector == 'OR' and len(node.children) == 0:
            raise EmptyResultSet()

        for and_branch in node.children[:]:
            if and_branch.is_leaf and and_branch.operator == "IN" and not len(and_branch.value):
                node.children.remove(and_branch)

            if not node.children:
                raise EmptyResultSet()

    remove_empty_in(where)

    def detect_conflicting_key_filter(node):
        assert node.connector == "OR"
        for and_branch in node.children[:]:
            # If we have a Root OR with leaf elements, we don't need to worry
            if and_branch.is_leaf:
                break

            pk_equality_found = None
            for child in and_branch.children:
                if child.column == "__key__" and child.operator == "=":
                    if pk_equality_found and pk_equality_found != child.value:
                        # Remove this AND branch as it's impossible to return anything
                        node.children.remove(and_branch)
                    else:
                        pk_equality_found = child.value
            if not node.children:
                raise EmptyResultSet()

    detect_conflicting_key_filter(query.where)

    return query
