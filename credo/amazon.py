from credo.errors import BadCredentialFile, NoCredentialsFound, BadCredential, UserQuit
from credo.asker import ask_user_for_half_life, ask_for_choice_or_new

from boto.iam.connection import IAMConnection
import datetime
import logging
import time
import boto
import sys
import os

log = logging.getLogger("credo.amazon")

class IamPair(object):
    def __init__(self, aws_access_key_id, aws_secret_access_key, create_epoch=None, half_life=None):
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key

        self._half_life = half_life
        self._create_epoch = create_epoch
        self._changed = False
        self.deleted = False

    @property
    def changed(self):
        """Get us value of _changed"""
        return self._changed

    def unchanged(self):
        """Set _changed to False"""
        self._changed = False

    @classmethod
    def from_environment(kls, create_epoch=None, half_life=None):
        """Get an IAMPair from our environment variables"""
        pair = kls(os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"], create_epoch, half_life)
        pair._connection = IAMConnection()
        return pair

    @property
    def connection(self):
        """Get a connection for these keys"""
        if not getattr(self, "_connection", None):
            self._connection = IAMConnection(self.aws_access_key_id, self.aws_secret_access_key)
        return self._connection

    @property
    def works(self):
        """Says whether this key is valid enough to get iam informations"""
        if self.deleted:
            return True
        if getattr(self, "_works", None) is False:
            return self._works
        self._get_user()
        return self._works

    def wait_till_works(self):
        """Wait till this iam pair works"""
        # Give amazon time to think about this
        start = time.time()
        while time.time() - start < 20:
            self._get_user(quiet=True)
            if self._works:
                break
            time.sleep(2)

    def ask_amazon_for_account(self):
        """Get the account id for this key"""
        self._get_user(get_cached=True)
        return getattr(self, "account_id", None)

    def ask_amazon_for_account_aliases(self):
        """Get the account aliases for this key"""
        self._get_user(get_cached=True)
        return getattr(self, "account_aliases", None)

    def ask_amazon_for_username(self):
        """Get the username for this key"""
        self._get_user(get_cached=True)
        return getattr(self, "username", None)

    @property
    def create_epoch(self):
        """Use our create_epoch or ask amazon for it"""
        if self._create_epoch:
            return self._create_epoch
        elif self.works:
            self._create_epoch = self.ask_amazon_for_create_epoch()
            self._changed = True
            return self._create_epoch
        else:
            return 0

    @property
    def half_life(self):
        """Use our half_life or ask user for one"""
        if not self._half_life:
            self._half_life = ask_user_for_half_life(self.aws_access_key_id)
            self._changed = True
        return self._half_life

    def set_half_life(self, half_life):
        """Record a new half_life"""
        if half_life != self._half_life:
            self._half_life = half_life
            self._changed = True

    def create_new(self):
        """Create a new iam pair to use"""
        log.info("Creating a new key")
        response = self.connection.create_access_key(self.ask_amazon_for_username())["create_access_key_response"]["create_access_key_result"]["access_key"]
        log.info("Created %s", response["access_key_id"])
        iam_pair = IamPair(str(response["access_key_id"]), str(response["secret_access_key"]), create_epoch=self.amazon_date_to_epoch(response["create_date"]))
        iam_pair.wait_till_works()
        return iam_pair

    def delete(self):
        """Delete this key pair from amazon"""
        return self.delete_access_key(self.aws_access_key_id)

    def delete_access_key(self, access_key):
        log.info("Deleting a key\taccess_key_id=%s", access_key)
        if access_key == self.aws_access_key_id:
            self.deleted = True
            self._changed = True
        return self.connection.delete_access_key(access_key)

    def find_other_access_keys(self):
        """Find all the access_keys for this user"""
        keys = self.connection.get_all_access_keys(self.ask_amazon_for_username())["list_access_keys_response"]["list_access_keys_result"]["access_key_metadata"]
        return [str(key["access_key_id"]) for key in keys]

    def ask_amazon_for_create_epoch(self):
        """Return our create_epoch"""
        username = self.ask_amazon_for_username()
        access_keys = self.connection.get_all_access_keys(username)["list_access_keys_response"]["list_access_keys_result"]["access_key_metadata"]
        create_date = [key for key in access_keys if key["access_key_id"] == self.connection.aws_access_key_id][0]['create_date']
        return self.amazon_date_to_epoch(create_date)

    def amazon_date_to_epoch(self, create_date):
        """Convert create_date from amazon into a create_epoch"""
        dt = boto.utils.parse_ts(create_date)
        return (dt - datetime.datetime(1970, 1, 1)).total_seconds()

    def _get_user(self, get_cached=False, quiet=False):
        """
        Get user details from this key and set
        self._working
        self.username
        self.account_id
        """
        try:
            if getattr(self, "_got_user", None) is None or not get_cached:
                details = self.connection.get_user()["get_user_response"]["get_user_result"]["user"]
                aliases = self.connection.get_account_alias()["list_account_aliases_response"]["list_account_aliases_result"]["account_aliases"]
                self._invalid = False
                self._works = True
                self._got_user = True
                self.username = details["user_name"]

                # arn is arn:aws:iam::<account_id>:<other>
                self.account_id = details["arn"].split(":")[4]
                self.account_aliases = aliases
        except boto.exception.BotoServerError as error:
            self._works = False
            self._got_user = False
            self._connection = None
            if error.status == 403 and error.code in ("InvalidClientTokenId", "SignatureDoesNotMatch"):
                self._invalid = True
                if not quiet:
                    log.info("Found invalid access key and secret key combination\taccess_key=%s\terror_code=%s\terror=%s", self.aws_access_key_id, error.code, error.message)
                return
            raise

    def expired(self):
        """Say whether the age of this key is past twice it's half life"""
        return self.age > self.half_life * 2

    def past_half_life(self):
        """Say whether the age of this key is past it's half life"""
        return self.age > self.half_life

    @property
    def age(self):
        """Age is time since it's create_epoch"""
        return time.time() - self.create_epoch

class AmazonKey(object):
    """Represents the information and meta information required for amazon credentials"""
    def __init__(self, key_info, credential_path):
        self.key_info = key_info
        self.credential_path = credential_path

    @property
    def crypto(self):
        """Proxy credential_path"""
        return self.credential_path.crypto

    @property
    def changed(self):
        """Say whether this key has changed"""
        if self.iam_pair:
            return self.iam_pair.changed
        else:
            return False

    def unchanged(self):
        """Set the key as unchanged"""
        self._changed = False
        if self.iam_pair:
            self.iam_pair.unchanged()

    @classmethod
    def using(kls, iam_pair, credential_path):
        """Create an AmazonKey from the provided details"""
        if not iam_pair.works:
            raise BadCredential()

        key_info = {"fingerprints": None, "create_epoch": iam_pair.create_epoch, "half_life": iam_pair.half_life}
        key = AmazonKey(key_info, credential_path)
        key._decrypted = [(iam_pair.aws_access_key_id, iam_pair.aws_secret_access_key)]
        return key

    def basic_validation(self):
        """Make sure the keys have basic requirements"""
        if "fingerprints" not in self.key_info:
            return "No fingerprints for this key"
        if not isinstance(self.fingerprints, dict):
            return "Fingerprints for this key are not a dictionary"
        if not self.crypto.decryptable(self.fingerprints):
            return "No private key can decrypt secrets"
        if any(not all(attr in value for attr in ("secret", "data", "verifier")) for value in self.fingerprints.values()):
            return "One or more of the fingeprints doesn't contain secret, data and verifier"

    @property
    def fingerprints(self):
        """Get our fingerprints from key_info"""
        if "fingerprints" not in self.key_info:
            self.key_info["fingerprints"] = {}
        return self.key_info["fingerprints"]

    def credentials(self):
        """Goes through our fingerprints and yields all our decryptable credentials as [aws_access_key_id, aws_secret_access_key]"""
        if getattr(self, "_decrypted", None):
            for key in self._decrypted:
                yield key
            return

        decrypted = self.crypto.decrypt_by_fingerprint(self.fingerprints, self.verifier)
        if decrypted:
            yield decrypted["aws_access_key_id"], decrypted["aws_secret_access_key"]

    @property
    def iam_pair(self):
        """Find the first verified iam pair"""
        if not getattr(self, "_iam_pair", None):
            for aws_access_key_id, aws_secret_access_key in self.credentials():
                self._iam_pair = IamPair(aws_access_key_id, aws_secret_access_key, self.key_info.get("create_epoch"), self.key_info.get("half_life"))
                break
        return getattr(self, "_iam_pair", None)

    @property
    def encrypted_values(self):
        """
        Return this key as a dictionary of {"fingerprints": {<fingerprint>: <info>, ...}, <other_options>}

        Where <info> is {"aws_access_key_id", "aws_secret_access_key"}

        and <other_options> includes {"create_epoch"}
        """
        fingerprints = self.crypto.fingerprinted({"aws_access_key_id": self.iam_pair.aws_access_key_id, "aws_secret_access_key": self.iam_pair.aws_secret_access_key})
        return {"fingerprints": fingerprints, "create_epoch": self.iam_pair.create_epoch, "half_life": self.iam_pair.half_life}

    def verifier(self, data, iam_pair=None):
        """Say that these values is an amazon key for this account and user"""
        iam_pair = IamPair(aws_access_key_id=data.get("aws_access_key_id"), aws_secret_access_key=data.get("aws_secret_access_key"))
        if not iam_pair.works:
            return False

        account = self.credential_path.account.account_id(iam_pair=iam_pair)
        username = self.credential_path.user.username(iam_pair=iam_pair)
        return account == iam_pair.ask_amazon_for_account() and username == iam_pair.ask_amazon_for_username()

class AmazonKeys(object):
    """Collection of Amazon keys"""
    type = "amazon"

    def __init__(self, keys, credential_path):
        if not keys:
            keys = []

        self.credential_path = credential_path
        self.keys = [AmazonKey(key, credential_path) for key in keys]
        self._changed = False

    @property
    def crypto(self):
        """Proxy credential_path"""
        return self.credential_path.crypto

    def __iter__(self):
        """Iterate through the keys"""
        return iter(self.keys)

    def __len__(self):
        """Return how many keys we have"""
        return len(self.keys)

    def add(self, iam_pair):
        """Add a key"""
        key = AmazonKey.using(iam_pair, self.credential_path)
        if not key.iam_pair or not key.iam_pair.works:
            raise BadCredential()

        if key.iam_pair.aws_access_key_id not in self.access_keys:
            self.keys.append(key)
            self._changed = True

    @property
    def changed(self):
        """Say whether there has been any changes"""
        return self._changed or any(key.changed for key in self.keys)

    def unchanged(self):
        """Reset changed on everything"""
        self._changed = False
        for key in self.keys:
            key.unchanged()

    @property
    def iam_pair(self):
        """Find the youngest iam pair"""
        keys = list(key for key in self.keys if key.iam_pair and key.iam_pair.works)
        if keys:
            return sorted(keys, key=lambda k: k.iam_pair.create_epoch, reverse=True)[0].iam_pair

    def basic_keys_validation(self):
        """Do some basic validation of our keys"""
        errors = []
        for index, key in enumerate(self.keys):
            nxt = key.basic_validation()
            if nxt:
                errors.append("key {0}: {1}".format(index+1, nxt))

        if errors:
            raise BadCredentialFile("Some of the keys were not valid", errors=errors)

    @property
    def encrypted_values(self):
        """Return our keys as a dictionary with encrypted values"""
        result = []
        access_keys = []
        log.info("Making encrypted values for some keys")
        for key in self.keys:
            if key.iam_pair and key.iam_pair.works and not key.iam_pair.deleted:
                result.append(key.encrypted_values)
                access_keys.append(key.iam_pair.aws_access_key_id)
            else:
                access_key=""
                if key.iam_pair:
                    access_key="\taccess_key={0}".format(list(key.credentials())[0][0])
                log.info("Not saving invalid credentials%s", access_key)

        log.info("Made encrypted values for %s keys using %s public keys", len(result), len(self.crypto.public_key_fingerprints))
        return result, access_keys

    @property
    def access_keys(self):
        """Return all the access keys we know about"""
        for key in self.keys:
            iam_pair = key.iam_pair
            if iam_pair and iam_pair.works:
                yield iam_pair.aws_access_key_id

    def exports(self):
        """Return a list of (key, value) exports that would be necessary for the main iam_pair"""
        iam_pair = self.iam_pair
        if not iam_pair:
            raise NoCredentialsFound()

        return [
              ("AWS_ACCESS_KEY_ID", iam_pair.aws_access_key_id)
            , ("AWS_SECRET_ACCESS_KEY", iam_pair.aws_secret_access_key)
            ]

    def add_key(self, iam_pair):
        """Create a new key and add to our collection"""
        key = AmazonKey.using(iam_pair, self.credential_path)
        self.keys.append(key)
        self._changed = True
        return key

    def deal_with_unknown_key(self, access_key, valid_iam_pair):
        """Work out what to do with this key credo doesn't know about"""
        while True:
            quit_choice = "Quit"
            delete_choice = "Delete key"
            choice = ask_for_choice_or_new(
                  "action for dealing with an access_key credo doesn't know about ({0})".format(access_key)
                , [quit_choice, delete_choice]
                )

            if choice == quit_choice:
                raise UserQuit
            elif choice == delete_choice:
                valid_iam_pair.delete_access_key(access_key)
                return
            else:
                iam_pair = IamPair(access_key, choice)
                if iam_pair.works:
                    half_life = ask_user_for_half_life(iam_pair.aws_access_key_id)
                    iam_pair.set_half_life(half_life)
                    key = self.add_key(iam_pair)
                    if key:
                        return key
                else:
                    print >> sys.stderr, "The secret key you entered was not valid"

    def needs_rotation(self):
        """Say whether the current keys that we know about need any rotation"""
        working = []
        for key in self.keys:
            if key.iam_pair and key.iam_pair.works:
                working.append(key)
                if key.iam_pair.past_half_life() or key.iam_pair.expired():
                    return True

        # Only need rotation if we have no working keys
        return len(working) == 0

    def rotate(self):
        """Rotate the keys and return whether any of them changed"""
        while True:
            # Keep looking at the keys until we have no more surprises
            counts = {"created": 0, "deleted": 0, "removed": 0, "resolved": 0}
            usable = []
            to_remain = []
            for_deletion = []
            for_rotation = []

            known = []

            for key in self.keys:
                if not key.iam_pair or not key.iam_pair.works:
                    counts["removed"] += 1
                else:
                    usable.append(key.iam_pair)
                    known.append(key.iam_pair.aws_access_key_id)
                    if key.iam_pair.expired():
                        counts["deleted"] += 1
                        for_deletion.append(key.iam_pair)
                    elif key.iam_pair.past_half_life():
                        counts["created"] += 1
                        for_rotation.append(key.iam_pair)
                        to_remain.append(key.iam_pair)
                    else:
                        to_remain.append(key.iam_pair)

            if not to_remain:
                counts["created"] += 1

            extras = []
            if any(usable):
                others = usable[0].find_other_access_keys()
                for access_key in others:
                    if access_key not in known:
                        new_key = self.deal_with_unknown_key(access_key, usable[0])
                        counts["resolved"] += 1
                        if new_key:
                            extras.append(new_key)

            if not extras:
                break

        if not any(counts.values()):
            return False
        else:
            self._changed = True

        log.info("Rotation resulted in creating %s keys, deleting %s keys, removing %s stale keys and resolving %s unknown keys"
            , counts["created"], counts["deleted"], counts["removed"], counts["resolved"]
            )

        if not any(usable):
            # All the keys will be removed when we save
            # Up to code after that to complain there is nothing to create new keys with
            for key in for_deletion:
                key.delete()
        else:
            if for_deletion and to_remain:
                # Make sure we have room for creating a new key
                deleting = for_deletion.pop(0)
                deleting.delete()
                usable = [pair for pair in usable if pair is not deleting]

            if for_rotation or not to_remain:
                if len(to_remain) > 1:
                    oldest = sorted(to_remain, key=lambda k: k.create_epoch)[0]
                    oldest.delete()
                    usable = [pair for pair in usable if pair is not oldest]

                iam_pair = usable[0].create_new()
                iam_pair.set_half_life(ask_user_for_half_life(access_key))
                self.add_key(iam_pair)

            for key in for_deletion:
                # Delete the other keys marked for deletion
                key.delete()

        return True

