from cumulusci.robotframework.pageobjects import ListingPage, DetailPage
from cumulusci.robotframework.pageobjects import pageobject


@pageobject("Listing", "Contact")
class ContactListingPage(ListingPage):
    object_name = "Contact"

    def click_contact_link(self, name):
        self.selenium.click_link('xpath://a[@title="{}"]'.format(name))
        self.salesforce.wait_until_loading_is_complete()


@pageobject("Detail", "Contact")
class ContactDetailPage(DetailPage):
    object_name = "Contact"

    def some_keyword(self):
        self.builtin.log("some keyword; object name is {}".format(self.object_name))
        self.selenium.capture_page_screenshot()
